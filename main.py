import os
import requests
import json
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

# --- Configuration ---
# Get your Telegram Bot Token from environment variables (important for Railway deployment)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Mail.tm API base URL
MAILTM_API_URL = "https://api.mail.tm"

# --- Global Variables to store user's current temp email ---
# This is a simple in-memory storage. For a production bot with many users,
# you would need a proper database (like SQLite, PostgreSQL, Redis)
# to persist user email data across restarts and for multiple users.
user_emails = {} # {chat_id: {"address": "...", "id": "...", "token": "..."}}

# --- Mail.tm API Functions ---

async def get_domains():
    """Fetches available Mail.tm domains."""
    try:
        response = requests.get(f"{MAILTM_API_URL}/domains")
        response.raise_for_status()  # Raise an exception for HTTP errors (e.g., 404, 500)

        if not response.text: # Check if response body is empty
            print("Mail.tm /domains endpoint returned empty response.")
            return []

        data = response.json() # Changed variable name from 'domains' to 'data'

        # --- THIS IS THE CRUCIAL CHANGE ---
        # Check if 'data' is a dictionary and contains 'hydra:member' key
        if isinstance(data, dict) and 'hydra:member' in data:
            domains_list = data['hydra:member']
        else:
            # Fallback if 'hydra:member' isn't found or 'data' isn't a dict.
            # This covers cases where it might be a direct list of domains, or unexpected format.
            print(f"Mail.tm /domains endpoint returned unexpected top-level data structure: {data}")
            if isinstance(data, list): # Check if it's already a list (might happen)
                domains_list = data
            else:
                return [] # Cannot process this format

        # Ensure 'domains_list' is actually a list/array
        if not isinstance(domains_list, list):
            print(f"Mail.tm /domains endpoint 'hydra:member' data is not a list: {domains_list}")
            return []

        if not domains_list:
            print("Mail.tm /domains endpoint returned an empty list of domains (or hydra:member was empty).")
            return []

        # Extract domain names from the list of domain objects
        # Using a list comprehension with a check for the 'domain' key
        return [d["domain"] for d in domains_list if isinstance(d, dict) and "domain" in d]

    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error fetching domains: {e.response.status_code} - {e.response.text}")
        return []
    except requests.exceptions.ConnectionError as e:
        print(f"Connection Error fetching domains: {e}")
        return []
    except requests.exceptions.Timeout as e:
        print(f"Timeout Error fetching domains: {e}")
        return []
    except requests.exceptions.RequestException as e:
        print(f"Generic Request Error fetching domains: {e}")
        return []
    except json.JSONDecodeError as e:
        print(f"JSON Decode Error fetching domains: {e} - Response text: {response.text}")
        return []
    except Exception as e:
        print(f"An unexpected error occurred in get_domains: {e}")
        return []
async def create_account(username=None, domain=None):
    """Creates a new temporary email account."""
    if not domain:
        domains = await get_domains()
        if not domains:
            return None, "Could not fetch domains. Please try again later."
        domain = domains[0] # Use the first available domain by default

    # If no username is provided, Mail.tm generates a random one.
    # We can also generate a random one if we want more control.
    if not username:
        import uuid
        username = str(uuid.uuid4()).split('-')[0] # Simple random string

    try:
        payload = {"address": f"{username}@{domain}", "password": "temp_password"} # Password is required but not really used by us
        response = requests.post(f"{MAILTM_API_URL}/accounts", json=payload)
        response.raise_for_status()
        account_data = response.json()
        
        # Get authentication token for the new account
        token_response = requests.post(f"{MAILTM_API_URL}/token", json={"address": account_data["address"], "password": "temp_password"})
        token_response.raise_for_status()
        token_data = token_response.json()

        return {
            "address": account_data["address"],
            "id": account_data["id"],
            "token": token_data["token"]
        }, None
    except requests.exceptions.RequestException as e:
        print(f"Error creating account: {e}")
        if response.status_code == 422: # Unprocessable Entity, often means address already exists
            return None, f"Could not create address. It might already exist or the domain is invalid. Try generating a new one."
        return None, f"Failed to create temporary email: {e}"

async def get_messages(account_id, token):
    """Fetches messages for a given temporary email account."""
    try:
        headers = {"Authorization": f"Bearer {token}"}
        response = requests.get(f"{MAILTM_API_URL}/accounts/{account_id}/messages", headers=headers)
        response.raise_for_status()
        messages = response.json()
        return messages
    except requests.exceptions.RequestException as e:
        print(f"Error fetching messages: {e}")
        return []

async def get_message_content(account_id, message_id, token):
    """Fetches the content of a specific message."""
    try:
        headers = {"Authorization": f"Bearer {token}"}
        response = requests.get(f"{MAILTM_API_URL}/accounts/{account_id}/messages/{message_id}", headers=headers)
        response.raise_for_status()
        message_content = response.json()
        return message_content
    except requests.exceptions.RequestException as e:
        print(f"Error fetching message content: {e}")
        return None

async def delete_account(account_id, token):
    """Deletes a temporary email account."""
    try:
        headers = {"Authorization": f"Bearer {token}"}
        response = requests.delete(f"{MAILTM_API_URL}/accounts/{account_id}", headers=headers)
        response.raise_for_status()
        return True, None
    except requests.exceptions.RequestException as e:
        print(f"Error deleting account: {e}")
        return False, f"Failed to delete account: {e}"

# --- Telegram Bot Command Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message and instructions."""
    await update.message.reply_text(
        "Hello! I'm your Temp Mail Bot. I can generate temporary email addresses for you.\n\n"
        "Use /generate to get a new temporary email address.\n"
        "Use /inbox to check for new messages in your current temporary email's inbox.\n"
        "Use /delete to delete your current temporary email address."
    )

async def generate_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generates a new temporary email address for the user."""
    chat_id = update.effective_chat.id
    
    # If an email already exists, offer to delete it first
    if chat_id in user_emails:
        keyboard = [[
            InlineKeyboardButton("Yes, generate new", callback_data="confirm_generate"),
            InlineKeyboardButton("No, keep current", callback_data="cancel_generate")
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"You already have an active temporary email: `{user_emails[chat_id]['address']}`. "
            "Generating a new one will delete the old one. Are you sure you want to proceed?",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        return

    await update.message.reply_text("Generating a new temporary email address, please wait...")
    
    account_info, error = await create_account()
    if account_info:
        user_emails[chat_id] = account_info
        await update.message.reply_text(
            f"Your new temporary email address is:\n`{account_info['address']}`\n\n"
            "Use /inbox to check for messages."
            "Remember, this is temporary and emails are usually deleted after some time by the service itself.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"Sorry, I couldn't generate a temporary email address at this time. {error}")

async def inbox(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Checks the inbox of the current temporary email address."""
    chat_id = update.effective_chat.id

    if chat_id not in user_emails:
        await update.message.reply_text("You don't have an active temporary email address. Use /generate to get one.")
        return

    account_id = user_emails[chat_id]["id"]
    token = user_emails[chat_id]["token"]
    
    await update.message.reply_text("Checking your inbox, please wait...")
    
    messages = await get_messages(account_id, token)

    if messages:
        message_list = []
        for msg in messages:
            subject = msg.get('subject', 'No Subject')
            from_address = msg.get('from', {}).get('address', 'Unknown Sender')
            msg_id = msg.get('id')
            
            # Create a button for each message to view its content
            keyboard = [[InlineKeyboardButton(f"Subject: {subject} (From: {from_address})", callback_data=f"view_msg_{msg_id}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            message_list.append(f"• From: `{from_address}`\n  Subject: `{subject}`")
            await update.message.reply_text(
                f"**New Message**\n{message_list[-1]}",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        
        # Original single message summary (can be removed if individual messages are preferred)
        # await update.message.reply_text(
        #     "Here are your messages:\n\n" + "\n\n".join(message_list),
        #     parse_mode="Markdown"
        # )
    else:
        await update.message.reply_text("Your inbox is empty.")

async def delete_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deletes the current temporary email address."""
    chat_id = update.effective_chat.id

    if chat_id not in user_emails:
        await update.message.reply_text("You don't have an active temporary email address to delete.")
        return

    keyboard = [[
        InlineKeyboardButton("Yes, delete it", callback_data="confirm_delete"),
        InlineKeyboardButton("No, keep it", callback_data="cancel_delete")
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"Are you sure you want to delete your current temporary email address: `{user_emails[chat_id]['address']}`?",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles inline keyboard button presses."""
    query = update.callback_query
    chat_id = query.message.chat_id
    await query.answer() # Acknowledge the callback query

    if query.data == "confirm_delete":
        if chat_id not in user_emails:
            await query.edit_message_text("No active email to delete.")
            return

        account_id = user_emails[chat_id]["id"]
        token = user_emails[chat_id]["token"]
        
        success, error = await delete_account(account_id, token)
        if success:
            del user_emails[chat_id]
            await query.edit_message_text("Your temporary email address has been deleted successfully.")
        else:
            await query.edit_message_text(f"Failed to delete your temporary email address. {error}")
    
    elif query.data == "cancel_delete":
        await query.edit_message_text("Deletion cancelled. Your temporary email address remains active.")

    elif query.data == "confirm_generate":
        # Delete old email first
        if chat_id in user_emails:
            account_id = user_emails[chat_id]["id"]
            token = user_emails[chat_id]["token"]
            success, error = await delete_account(account_id, token)
            if not success:
                await query.edit_message_text(f"Could not delete old email: {error}. Please try /generate again.")
                return
            del user_emails[chat_id] # Remove from in-memory storage after successful deletion

        # Then generate new
        await query.edit_message_text("Generating a new temporary email address, please wait...")
        account_info, error = await create_account()
        if account_info:
            user_emails[chat_id] = account_info
            await query.edit_message_text(
                f"Your new temporary email address is:\n`{account_info['address']}`\n\n"
                "Use /inbox to check for messages."
                "Remember, this is temporary and emails are usually deleted after some time by the service itself.",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(f"Sorry, I couldn't generate a temporary email address at this time. {error}")

    elif query.data == "cancel_generate":
        await query.edit_message_text("New email generation cancelled. Your current email remains active.")

    elif query.data.startswith("view_msg_"):
        message_id = query.data.split("_")[2]
        if chat_id not in user_emails:
            await query.edit_message_text("Session expired. Please generate a new email.")
            return
        
        account_id = user_emails[chat_id]["id"]
        token = user_emails[chat_id]["token"]

        message_content = await get_message_content(account_id, message_id, token)
        if message_content:
            subject = message_content.get('subject', 'No Subject')
            from_address = message_content.get('from', {}).get('address', 'Unknown Sender')
            text_body = message_content.get('text', message_content.get('html', 'No content available')).strip()
            
            # Limit message length to avoid Telegram API limits
            if len(text_body) > 4000:
                text_body = text_body[:3900] + "\n\n... (Message truncated)"

            await query.edit_message_text(
                f"**Subject:** `{subject}`\n"
                f"**From:** `{from_address}`\n\n"
                f"**Content:**\n```\n{text_body}\n```",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text("Could not retrieve message content.")

def main() -> None:
    """Starts the bot."""
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN environment variable not set.")
        print("Please set it before running the bot.")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Register command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("generate", generate_email))
    application.add_handler(CommandHandler("inbox", inbox))
    application.add_handler(CommandHandler("delete", delete_email))
    
    # Register callback query handler for inline buttons
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    print("Bot started. Listening for updates...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
