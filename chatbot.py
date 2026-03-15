import asyncio
import logging
import configparser
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters
from ChatGPT_HKBU import ChatGPT

gpt = None
# Configure logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # await update.message.reply_text(response)
    logging.info("UPDATE: " + str(update))
    loading_message = await update.message.reply_text('Thinking...')

    try:
        # Run the blocking submit call in a separate thread to avoid blocking the event loop
        response = await asyncio.to_thread(gpt.submit, update.message.text)
    except Exception as e:
        logging.error(f"Error calling ChatGPT: {e}")
        response = "Sorry, I encountered an error while contacting the AI server."

    # send the response to the Telegram box client
    await loading_message.edit_text(response)

async def main():
    # Load the configuration data from file
    logging.info('INIT: Loading configuration...')
    config = configparser.ConfigParser()
    config.read('config.ini')
    token = config['TELEGRAM']['ACCESS_TOKEN']
    global gpt
    gpt = ChatGPT(config)
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

    # Register handlers
    logging.info('INIT: Registering handlers...')
    app.add_handler(CommandHandler("stop", stop_command))
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

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
