import os

print("--- ডায়াগনস্টিক টেস্ট শুরু ---")

token = os.environ.get("TELEGRAM_BOT_TOKEN")
gemini_key = os.environ.get("GEMINI_API_KEY")
db_url = os.environ.get("DATABASE_URL")

print(f"TELEGRAM_BOT_TOKEN: {token}")
print(f"GEMINI_API_KEY: {gemini_key}")
print(f"DATABASE_URL: {db_url}")

if not token or not gemini_key or not db_url:
    print("\n---!!! ERROR: ভেরিয়েবল পাওয়া যায়নি !!!---")
else:
    print("\n--- ✅ SUCCESS: সব ভেরিয়েবল পাওয়া গেছে ---")
    
print("--- ডায়াগনস্টিক টেস্ট শেষ ---")