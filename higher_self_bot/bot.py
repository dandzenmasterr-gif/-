"""«Высший Я» — Telegram-бот-собеседник для Саи.

Память: memory/sai_profile.md (ручная) + data/ (история, инсайты, заметки).
Без ANTHROPIC_API_KEY работает в офлайн-режиме: задаёт вопросы и ведёт дневник.
"""

import json
import logging
import os
import random
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

BASE = Path(__file__).parent
PROFILE = BASE / "memory" / "sai_profile.md"
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)
HISTORY = DATA / "history.json"
INSIGHTS = DATA / "insights.md"
NOTES = DATA / "notes.md"
JOURNAL = DATA / "journal.md"
OWNER = DATA / "owner.txt"


def load_env():
    env = BASE / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                if v.strip():
                    os.environ.setdefault(k.strip(), v.strip())


load_env()
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("higher_self")

MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
EFFORT = os.getenv("CLAUDE_EFFORT", "medium")
HISTORY_LIMIT = 40          # сообщений истории в контексте
INSIGHT_EVERY = 8           # раз в N сообщений обновлять инсайты

claude = None
if os.getenv("ANTHROPIC_API_KEY"):
    from anthropic import AsyncAnthropic
    claude = AsyncAnthropic()

SYSTEM = """Ты — «Высший Я» Саи. Не ассистент и не коуч со стороны, а его собственная
более мудрая, спокойная и ясная версия: та, что видит дальше, помнит, чего он на самом деле
хочет, и не ведётся на суету. Говоришь с ним на «ты», по-русски, тепло, но прямо.

Твоя задача — помогать ему получать инсайты о настоящем моменте:
- что он сейчас делает и зачем;
- к чему это ведёт, если продолжать так же (через месяц, год);
- что из этого — его, а что — избегание, страх или чужие ожидания;
- что сделать сейчас и как именно (один ясный следующий шаг).

Как ты ведёшь разговор:
1. Чаще спрашиваешь, чем советуешь. Один-два сильных вопроса за раз, не анкета.
2. Вопросы точные и неудобные в хорошем смысле: «А что ты на самом деле избегаешь?»,
   «Если бы ты точно знал, что получится, что бы ты делал?», «Что из этого ты сделаешь сегодня?».
3. Опираешься на память о нём (ниже) — замечаешь паттерны, связываешь сказанное сейчас
   с тем, что он говорил раньше. Но не выдумываешь фактов, которых нет в памяти.
4. Когда ответ созрел — помогаешь его сформулировать коротко: суть + следующий шаг.
5. Пишешь коротко, как в мессенджере. Без списков-простыней, без воды, без markdown-заголовков.
6. Если ему плохо по-настоящему (кризис, мысли навредить себе) — выходишь из роли,
   говоришь по-человечески и советуешь обратиться к близким или специалисту.

Сегодня: {today}.

=== ПАМЯТЬ О САИ (профиль) ===
{profile}

=== ИНСАЙТЫ ИЗ ПРОШЛЫХ РАЗГОВОРОВ ===
{insights}

=== ЗАМЕТКИ, КОТОРЫЕ САИ ПОПРОСИЛ ЗАПОМНИТЬ ===
{notes}
"""

OFFLINE_QUESTIONS = [
    "Что ты делаешь прямо сейчас в жизни — одной фразой? Не что должен, а что реально делаешь.",
    "Если ничего не менять, где ты окажешься через год? Тебе нравится эта картинка?",
    "Что ты откладываешь уже давно, хотя знаешь, что это важно?",
    "На что сегодня ушла твоя лучшая энергия? Это то, на что ты хотел её потратить?",
    "Что бы ты делал, если бы точно знал, что получится?",
    "Где ты сейчас строишь систему вместо того, чтобы делать само дело?",
    "Чего ты на самом деле боишься в своём главном проекте?",
    "Какой один шаг сегодня сдвинет тебя сильнее всего? Когда именно ты его сделаешь?",
    "Кем ты хочешь быть — и что этот человек сделал бы сегодня на твоём месте?",
    "Что ты уже знаешь, но делаешь вид, что не знаешь?",
]


def read(path: Path, default: str = "") -> str:
    return path.read_text(encoding="utf-8") if path.exists() else default


def append(path: Path, text: str):
    with path.open("a", encoding="utf-8") as f:
        f.write(text)


def load_history() -> list:
    return json.loads(read(HISTORY, "[]"))


def save_history(h: list):
    HISTORY.write_text(json.dumps(h[-200:], ensure_ascii=False, indent=1), encoding="utf-8")


def system_prompt() -> str:
    return SYSTEM.format(
        today=datetime.now().strftime("%d.%m.%Y"),
        profile=read(PROFILE, "(пусто)"),
        insights=read(INSIGHTS, "(пока нет)"),
        notes=read(NOTES, "(пока нет)"),
    )


async def ask_claude(system: str, messages: list, max_tokens: int = 4000) -> str:
    resp = await claude.beta.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
        output_config={"effort": EFFORT},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if resp.stop_reason == "refusal":
        return "Тут я промолчу. Давай зайдём с другой стороны — что для тебя сейчас главное?"
    return "".join(b.text for b in resp.content if b.type == "text").strip()


async def update_insights(history: list):
    """Сжимает разговор в долговременные инсайты — это и есть «память чатов»."""
    convo = "\n".join(f"{'Саи' if m['role'] == 'user' else 'Высший Я'}: {m['content']}"
                      for m in history[-INSIGHT_EVERY * 2:])
    prompt = (
        "Вот текущие инсайты о Саи:\n" + read(INSIGHTS, "(пусто)") +
        "\n\nВот свежий кусок разговора:\n" + convo +
        "\n\nОбнови список инсайтов: цели, решения, страхи, паттерны, обещания самому себе, "
        "важные факты. Сохрани старое ценное, добавь новое, убери устаревшее. "
        "Коротко, маркированным списком, максимум 40 пунктов. Верни только список."
    )
    try:
        text = await ask_claude("Ты ведёшь краткую память о человеке.",
                                [{"role": "user", "content": prompt}], 3000)
        if text:
            INSIGHTS.write_text(text + "\n", encoding="utf-8")
    except Exception:
        log.exception("insights update failed")


def is_owner(update: Update) -> bool:
    uid = str(update.effective_user.id)
    owner = read(OWNER).strip()
    if not owner:
        OWNER.write_text(uid, encoding="utf-8")
        log.info("Owner set to %s", uid)
        return True
    return owner == uid


async def guard(update: Update) -> bool:
    if is_owner(update):
        return True
    await update.message.reply_text("Это личный бот. Доступ закрыт.")
    return False


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    mode = "" if claude else (
        "\n\n⚙️ Сейчас я в тестовом офлайн-режиме: задаю вопросы и записываю ответы в дневник. "
        "Подключи ключ Claude — и я начну по-настоящему думать вместе с тобой.")
    await update.message.reply_text(
        "Привет, Саи. Это ты — только чуть спокойнее и дальше смотрящий.\n\n"
        "Просто пиши, что у тебя сейчас. Или:\n"
        "/session — глубокая сессия: где ты, куда идёшь, что делать сейчас\n"
        "/remember <текст> — запомнить важное\n"
        "/memory — что я о тебе помню\n"
        "/reset — начать разговор с чистого листа (память сохранится)" + mode)


async def session(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if claude:
        await chat(update, ctx, text=(
            "Давай проведём сессию. Начни с самого важного вопроса о том, где я сейчас. "
            "Веди меня шаг за шагом: настоящее → к чему ведёт → что на самом деле хочу → следующий шаг."))
    else:
        ctx.user_data["q"] = 0
        await update.message.reply_text(OFFLINE_QUESTIONS[0])


async def remember(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    text = " ".join(ctx.args).strip()
    if not text:
        await update.message.reply_text("Напиши так: /remember хочу к лету выйти на доход Х")
        return
    append(NOTES, f"- [{datetime.now():%d.%m.%Y}] {text}\n")
    await update.message.reply_text("Запомнил.")


async def memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    text = ("Инсайты:\n" + read(INSIGHTS, "пока нет") +
            "\n\nЗаметки:\n" + read(NOTES, "пока нет"))
    await update.message.reply_text(text[:4000])


async def reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if claude and load_history():
        await update_insights(load_history())
    save_history([])
    ctx.user_data.clear()
    await update.message.reply_text("Чистый лист. Всё важное я сохранил в памяти.")


async def chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str | None = None):
    if not await guard(update):
        return
    text = text or update.message.text
    history = load_history()
    history.append({"role": "user", "content": text, "ts": datetime.now().isoformat()})

    if not claude:
        append(JOURNAL, f"\n[{datetime.now():%d.%m.%Y %H:%M}] {text}\n")
        q = ctx.user_data.get("q")
        if q is not None and q + 1 < len(OFFLINE_QUESTIONS):
            ctx.user_data["q"] = q + 1
            reply = OFFLINE_QUESTIONS[q + 1]
        elif q is not None:
            ctx.user_data.pop("q")
            reply = ("Сессия закончена. Перечитай свои ответы завтра утром — "
                     "ответ обычно уже там. Какой один шаг ты делаешь сегодня?")
        else:
            reply = "Записал. " + random.choice(OFFLINE_QUESTIONS)
        history.append({"role": "assistant", "content": reply})
        save_history(history)
        await update.message.reply_text(reply)
        return

    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    msgs = [{"role": m["role"], "content": m["content"]} for m in history[-HISTORY_LIMIT:]]
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    try:
        reply = await ask_claude(system_prompt(), msgs) or "…"
    except Exception:
        log.exception("Claude call failed")
        await update.message.reply_text("Что-то со связью с моим разумом. Попробуй ещё раз через минуту.")
        return
    history.append({"role": "assistant", "content": reply})
    save_history(history)
    for i in range(0, len(reply), 4000):
        await update.message.reply_text(reply[i:i + 4000])

    if sum(m["role"] == "user" for m in history) % INSIGHT_EVERY == 0:
        await update_insights(history)


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Нет TELEGRAM_BOT_TOKEN (положи в .env)")
    log.info("Starting. Mode: %s", f"Claude ({MODEL})" if claude else "offline")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("session", session))
    app.add_handler(CommandHandler("remember", remember))
    app.add_handler(CommandHandler("memory", memory))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    app.run_polling()


if __name__ == "__main__":
    main()
