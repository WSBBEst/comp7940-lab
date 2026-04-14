import httpx
import configparser
import logging
import os

# A simple client for the ChatGPT REST API
class ChatGPT:
    def __init__(self, config):
        def get_config_value(section: str, key: str, env_var: str, *, default: str | None = None, required: bool = False) -> str | None:
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

        # Read API configuration values from the ini file
        api_key = get_config_value("CHATGPT", "API_KEY", "CHATGPT_API_KEY", required=True)
        base_url = get_config_value("CHATGPT", "BASE_URL", "CHATGPT_BASE_URL", default="https://genai.hkbu.edu.hk/api/v0/rest", required=True)
        model = get_config_value("CHATGPT", "MODEL", "CHATGPT_MODEL", default="gpt-5-mini", required=True)
        api_ver = get_config_value("CHATGPT", "API_VER", "CHATGPT_API_VER", default="2024-12-01-preview", required=True)

        # Construct the full REST endpoint URL for chat completions
        self.url = f'{base_url}/deployments/{model}/chat/completions?api-version={api_ver}'

        # Set HTTP headers required for authentication and JSON payload
        self.headers = {
            "accept": "application/json",
            "Content-Type": "application/json",
            "api-key": api_key,
            # Add User-Agent to mimic a browser, preventing some WAF/Firewall blocks
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        # Define the system prompt to guide the assistant’s behavior
        self.system_message = (
            'You are a helper! Your users are university students. '
            'Your replies should be conversational, informative, use simple words, and be straightforward.'
        )
        
        # Create an HTTP client with SSL verification disabled
        # httpx often handles proxies better than requests, especially in complex network environments
        self.client = httpx.Client(verify=False, timeout=60.0)

    def submit_with_system(self, user_message: str, system_message: str):
        # Build the conversation history: system + user message
        messages = [
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ]

        # Prepare the request payload with generation parameters
        payload = {
            "messages": messages,
            "temperature": 1,     # randomness of output (higher = more creative)
            "max_tokens": 150,    # maximum length of the reply
            "top_p": 1,           # nucleus sampling parameter
            "stream": False       # disable streaming, wait for full reply
        }    

        try:
            # Send the request to the ChatGPT REST API using httpx
            response = self.client.post(self.url, json=payload, headers=self.headers)

            # If successful, return the assistant’s reply text
            if response.status_code == 200:
                return response.json()['choices'][0]['message']['content']
            else:
                # Otherwise return error details
                return "Error: " + response.text
        except Exception as e:
            return f"Error connecting to ChatGPT: {str(e)}"

    def submit(self, user_message: str):
        return self.submit_with_system(user_message, self.system_message)
    

if __name__ == '__main__':
    # Load configuration from ini file
    config = configparser.ConfigParser()
    config.read('config.ini')    

    # Initialize ChatGPT client
    chatGPT = ChatGPT(config)

    # Simple REPL loop: read user input, send to ChatGPT, print reply
    while True:
        try:
            print('Input your query: ', end='')
            user_input = input()
            if user_input.lower() in ('exit', 'quit'):
                break
            response = chatGPT.submit(user_input)
            print(response)
        except KeyboardInterrupt:
            break
