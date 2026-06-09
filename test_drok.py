"""Quick test that DROK is reachable from this machine.

Run: .\venv\Scripts\python.exe test_drok.py
Make sure HF_TOKEN is set in your .env first.
"""
from dotenv import load_dotenv
load_dotenv()

import drok

print('Configured:', drok.is_configured())
print('---')
print('Q: What services does DigiRocket offer?')
print('A:', drok.ask_drok('What services does DigiRocket offer?'))
print('---')
print('Q (Hinglish): Bhai mujhe ek SEO report chahiye, kya tum help kar sakte ho?')
print('A:', drok.chat_reply('Bhai mujhe ek SEO report chahiye, kya tum help kar sakte ho?'))
print('---')
print('About blurb (Eyecandy Brow Salon / beauty):')
print(drok.about_digirocket_blurb(client_name='Eyecandy Brow Salon', industry='beauty'))
