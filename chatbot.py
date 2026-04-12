import asyncio
import logging
import configparser
import os
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

def init_db(database_url: str) -> psycopg.Connection:
    conn = psycopg.connect(database_url, autocommit=True)
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_logs (
                id BIGSERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
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
    return conn

def log_chat_event(
    conn: psycopg.Connection,
    *,
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
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
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

async def main():
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
    # Create an Application for your bot
    logging.info('INIT: Connecting the Telegram bot...')
    app = ApplicationBuilder().token(token).build()

    # Create an event to stop the bot gracefully
    stop_event = asyncio.Event()

    # Define a handler for the /stop command
    async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Stopping bot...")
        logging.info("Stopping bot via /stop command")
        stop_event.set()

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

    # Register handlers
    logging.info('INIT: Registering handlers...')
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, callback))

    # Start the bot
    logging.info('INIT: Initialization done!')
    
    # Explicitly initialize the application and start polling
    # This avoids "ExtBot is not properly initialized" errors
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    
    # Keep the bot running until stopped
    logging.info('Bot is running... Press Ctrl+C to stop.')
    
    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        logging.info("Stopping bot due to cancellation...")
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        if db_conn is not None:
            try:
                db_conn.close()
            except Exception:
                pass

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
