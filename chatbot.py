import asyncio
import logging
import configparser
import os
import re
import time
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters
from ChatGPT_HKBU import ChatGPT
import psycopg

gpt = None
db_conn = None
# Configure logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

def get_config_value(config: configparser.ConfigParser, section: str, key: str, env_var: str, *, default: str | None = None, required: bool = False) -> str | None:
    value = os.getenv(env_var)
    if value is not None and value != "":
        return value
    if config.has_option(section, key):
        raw = config.get(section, key)
        if raw != "":
            return raw
    if required:
        raise ValueError(f"Missing required config: env {env_var} or [{section}] {key}")
    return default

def normalize_interests(raw_text: str) -> list[str]:
    interests = []
    seen = set()
    for token in re.split(r"[,;/\n]+", raw_text.lower()):
        cleaned = " ".join(token.strip().split())
        if cleaned and cleaned not in seen:
            interests.append(cleaned)
            seen.add(cleaned)
    return interests[:10]

def format_interests(interests: list[str]) -> str:
    if not interests:
        return "None yet"
    return ", ".join(interests)

def get_display_name(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "Telegram user"
    name_parts = [user.first_name or "", user.last_name or ""]
    full_name = " ".join(part for part in name_parts if part).strip()
    if full_name:
        return full_name
    if user.username:
        return f"@{user.username}"
    return f"user-{user.id}"

def init_db(database_url: str) -> psycopg.Connection:
    conn = psycopg.connect(database_url, autocommit=True)
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_logs (
                id BIGSERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                update_id BIGINT,
                telegram_user_id BIGINT,
                telegram_chat_id BIGINT,
                telegram_message_id BIGINT,
                user_text TEXT,
                assistant_text TEXT,
                llm_model TEXT,
                latency_ms INTEGER,
                is_error BOOLEAN NOT NULL DEFAULT FALSE,
                error_message TEXT
            )
            """
        )
        cur.execute("ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS update_id BIGINT")
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS chat_logs_update_id_uniq
            ON chat_logs(update_id)
            WHERE update_id IS NOT NULL
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_profiles (
                telegram_user_id BIGINT PRIMARY KEY,
                username TEXT,
                display_name TEXT NOT NULL,
                interests_text TEXT NOT NULL DEFAULT '',
                bio_text TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    return conn

def upsert_user_profile(
    conn: psycopg.Connection,
    *,
    telegram_user_id: int,
    username: str | None,
    display_name: str,
    interests: list[str],
    bio_text: str | None,
):
    interests_text = ",".join(interests)
    clean_bio = (bio_text or "").strip() or None
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_profiles (
                telegram_user_id,
                username,
                display_name,
                interests_text,
                bio_text,
                updated_at
            )
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (telegram_user_id) DO UPDATE SET
                username = EXCLUDED.username,
                display_name = EXCLUDED.display_name,
                interests_text = EXCLUDED.interests_text,
                bio_text = EXCLUDED.bio_text,
                updated_at = now()
            """,
            (telegram_user_id, username, display_name, interests_text, clean_bio),
        )

def get_user_profile(conn: psycopg.Connection, telegram_user_id: int):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT telegram_user_id, username, display_name, interests_text, bio_text, updated_at
            FROM user_profiles
            WHERE telegram_user_id = %s
            """,
            (telegram_user_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    interests = normalize_interests(row[3])
    return {
        "telegram_user_id": row[0],
        "username": row[1],
        "display_name": row[2],
        "interests": interests,
        "bio_text": row[4],
        "updated_at": row[5],
    }

def find_matching_profiles(conn: psycopg.Connection, telegram_user_id: int, interests: list[str]):
    own_interest_set = set(interests)
    matches = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT telegram_user_id, username, display_name, interests_text, bio_text
            FROM user_profiles
            WHERE telegram_user_id <> %s
            ORDER BY updated_at DESC
            """,
            (telegram_user_id,),
        )
        for row in cur.fetchall():
            candidate_interests = normalize_interests(row[3])
            shared = sorted(own_interest_set.intersection(candidate_interests))
            if not shared:
                continue
            matches.append(
                {
                    "telegram_user_id": row[0],
                    "username": row[1],
                    "display_name": row[2],
                    "interests": candidate_interests,
                    "bio_text": row[4],
                    "shared": shared,
                    "score": len(shared),
                }
            )
    matches.sort(key=lambda item: (-item["score"], item["display_name"].lower()))
    return matches

def log_chat_event(
    conn: psycopg.Connection,
    *,
    update_id: int | None,
    telegram_user_id: int | None,
    telegram_chat_id: int | None,
    telegram_message_id: int | None,
    user_text: str | None,
    assistant_text: str | None,
    llm_model: str | None,
    latency_ms: int | None,
    is_error: bool,
    error_message: str | None,
):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO chat_logs (
                update_id,
                telegram_user_id,
                telegram_chat_id,
                telegram_message_id,
                user_text,
                assistant_text,
                llm_model,
                latency_ms,
                is_error,
                error_message
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (update_id) WHERE update_id IS NOT NULL DO NOTHING
            """,
            (
                update_id,
                telegram_user_id,
                telegram_chat_id,
                telegram_message_id,
                user_text,
                assistant_text,
                llm_model,
                latency_ms,
                is_error,
                error_message,
            ),
        )

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # await update.message.reply_text(response)
    logging.info("UPDATE: " + str(update))
    loading_message = await update.message.reply_text('Thinking...')
    start = time.perf_counter()
    error_message = None
    is_error = False

    try:
        # Run the blocking submit call in a separate thread to avoid blocking the event loop
        response = await asyncio.to_thread(gpt.submit, update.message.text)
    except Exception as e:
        logging.error(f"Error calling ChatGPT: {e}")
        response = "Sorry, I encountered an error while contacting the AI server."
        error_message = str(e)
        is_error = True

    # send the response to the Telegram box client
    await loading_message.edit_text(response)

    latency_ms = int((time.perf_counter() - start) * 1000)
    global db_conn
    if db_conn is not None:
        try:
            llm_model = os.getenv("CHATGPT_MODEL")
            log_chat_event(
                db_conn,
                update_id=update.update_id,
                telegram_user_id=(update.effective_user.id if update.effective_user else None),
                telegram_chat_id=(update.effective_chat.id if update.effective_chat else None),
                telegram_message_id=(update.message.message_id if update.message else None),
                user_text=(update.message.text if update.message else None),
                assistant_text=response,
                llm_model=llm_model,
                latency_ms=latency_ms,
                is_error=is_error,
                error_message=error_message,
            )
        except Exception as e:
            logging.error(f"DB logging failed: {e}")

def build_application(token: str):
    app = ApplicationBuilder().token(token).build()

    async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Stopping bot...")
        context.application.stop_running()

    async def setprofile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        global db_conn
        if db_conn is None or update.effective_user is None:
            await update.message.reply_text("DB is not configured.")
            return
        raw = " ".join(context.args).strip()
        if not raw:
            await update.message.reply_text(
                "Usage: /setprofile ai, cloud, python | Looking for study partners"
            )
            return

        if "|" in raw:
            interests_part, bio_part = raw.split("|", 1)
        else:
            interests_part, bio_part = raw, ""
        interests = normalize_interests(interests_part)
        if not interests:
            await update.message.reply_text(
                "Please provide at least one interest. Example: /setprofile ai, cloud, python"
            )
            return

        upsert_user_profile(
            db_conn,
            telegram_user_id=update.effective_user.id,
            username=update.effective_user.username,
            display_name=get_display_name(update),
            interests=interests,
            bio_text=bio_part.strip(),
        )
        await update.message.reply_text(
            "Profile saved.\n"
            f"- interests: {format_interests(interests)}\n"
            "- next: use /match to find similar users or /recommend for activity ideas"
        )

    async def myprofile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        global db_conn
        if db_conn is None or update.effective_user is None:
            await update.message.reply_text("DB is not configured.")
            return
        profile = get_user_profile(db_conn, update.effective_user.id)
        if profile is None:
            await update.message.reply_text(
                "No profile yet. Use /setprofile ai, cloud, python | short bio"
            )
            return
        bio_line = profile["bio_text"] or "Not provided"
        await update.message.reply_text(
            "Your profile\n"
            f"- name: {profile['display_name']}\n"
            f"- interests: {format_interests(profile['interests'])}\n"
            f"- bio: {bio_line}"
        )

    async def match_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        global db_conn
        if db_conn is None or update.effective_user is None:
            await update.message.reply_text("DB is not configured.")
            return
        profile = get_user_profile(db_conn, update.effective_user.id)
        if profile is None or not profile["interests"]:
            await update.message.reply_text(
                "Set your interests first with /setprofile ai, cloud, python"
            )
            return

        matches = find_matching_profiles(db_conn, update.effective_user.id, profile["interests"])
        if not matches:
            await update.message.reply_text(
                "No close matches yet. Ask another user to add a profile with /setprofile first."
            )
            return

        lines = ["Top matches"]
        for match in matches[:3]:
            contact = f"@{match['username']}" if match["username"] else match["display_name"]
            summary = ", ".join(match["shared"])
            bio_suffix = f" | bio: {match['bio_text']}" if match["bio_text"] else ""
            lines.append(f"- {contact} | shared interests: {summary}{bio_suffix}")
        await update.message.reply_text("\n".join(lines))

    async def recommend_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        global db_conn
        topic = " ".join(context.args).strip()
        if not topic and db_conn is not None and update.effective_user is not None:
            profile = get_user_profile(db_conn, update.effective_user.id)
            if profile is not None and profile["interests"]:
                topic = ", ".join(profile["interests"])
        if not topic:
            await update.message.reply_text(
                "Usage: /recommend ai hackathons\n"
                "Tip: if you save a profile first, /recommend can use your interests automatically."
            )
            return

        loading_message = await update.message.reply_text("Finding relevant activities...")
        system_prompt = (
            "You help university students discover relevant online activities. "
            "Suggest realistic activity types, student events, workshops, competitions, or study communities. "
            "Do not claim real-time dates or links unless they are provided."
        )
        user_prompt = (
            f"Student interests: {topic}\n"
            "Give 3 concise recommendations. For each item include: title, why it fits, and a simple first step. "
            "Keep the total response under 140 words."
        )
        response = await asyncio.to_thread(gpt.submit_with_system, user_prompt, system_prompt)
        if not response.startswith("Error:") and not response.startswith("Error connecting"):
            response += "\n\nTip: verify dates and links before joining."
        await loading_message.edit_text(response)

    async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        global db_conn
        if db_conn is None:
            await update.message.reply_text("DB is not configured.")
            return
        try:
            with db_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        COUNT(*)::bigint AS total,
                        COALESCE(SUM(CASE WHEN is_error THEN 1 ELSE 0 END), 0)::bigint AS errors,
                        COALESCE(AVG(latency_ms), 0)::float AS avg_latency_ms
                    FROM chat_logs
                    WHERE created_at > now() - interval '24 hours'
                    """
                )
                total, errors, avg_latency_ms = cur.fetchone()
            await update.message.reply_text(
                f"Last 24h stats\n"
                f"- total requests: {total}\n"
                f"- errors: {errors}\n"
                f"- avg latency (ms): {avg_latency_ms:.0f}"
            )
        except Exception as e:
            logging.error(f"Stats query failed: {e}")
            await update.message.reply_text("Stats query failed.")

    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("setprofile", setprofile_command))
    app.add_handler(CommandHandler("myprofile", myprofile_command))
    app.add_handler(CommandHandler("match", match_command))
    app.add_handler(CommandHandler("recommend", recommend_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("status", stats_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, callback))
    return app

def main():
    # Load the configuration data from file
    logging.info('INIT: Loading configuration...')
    config = configparser.ConfigParser()
    config.read('config.ini')
    token = get_config_value(config, "TELEGRAM", "ACCESS_TOKEN", "TELEGRAM_ACCESS_TOKEN", required=True)
    global gpt
    gpt = ChatGPT(config)
    database_url = get_config_value(config, "DATABASE", "URL", "DATABASE_URL", required=True)
    global db_conn
    db_conn = init_db(database_url)
    logging.info('INIT: Connecting the Telegram bot...')
    app = build_application(token)
    mode = (os.getenv("TELEGRAM_MODE") or "polling").lower()
    try:
        if mode == "webhook":
            port = int(os.getenv("PORT") or "8080")
            url_path = (os.getenv("TELEGRAM_WEBHOOK_PATH") or "telegram").lstrip("/")
            base_url = os.getenv("TELEGRAM_WEBHOOK_URL")
            if base_url is None or base_url == "":
                raise ValueError("Missing required config: env TELEGRAM_WEBHOOK_URL")
            webhook_url = base_url.rstrip("/") + "/" + url_path
            secret_token = os.getenv("TELEGRAM_WEBHOOK_SECRET") or None
            app.run_webhook(
                listen="0.0.0.0",
                port=port,
                url_path=url_path,
                webhook_url=webhook_url,
                secret_token=secret_token,
                drop_pending_updates=True,
            )
        else:
            app.run_polling(drop_pending_updates=True)
    finally:
        if db_conn is not None:
            try:
                db_conn.close()
            except Exception:
                pass

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
