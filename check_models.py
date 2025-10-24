import google.generativeai as genai

# --- আপনার কী এখানে দিন ---
GEMINI_API_KEY = "AIzaSyAVwCdnIDqK7bOwWbvSBK_UJCf6Ui3jA6Q"
# -------------------------

try:
    genai.configure(api_key=GEMINI_API_KEY)
    print("API Key কনফিগার করা হয়েছে। উপলব্ধ মডেলগুলো খোঁজা হচ্ছে...\n")

    print("--- আপনার কী দিয়ে এই মডেলগুলো ব্যবহার করা যাবে ---")
    for m in genai.list_models():
        # আমরা শুধু সেই মডেলগুলো খুঁজছি যা চ্যাট/টেক্সট জেনারেট করতে পারে
        if 'generateContent' in m.supported_generation_methods:
            print(m.name)
    print("--------------------------------------------------")

except Exception as e:
    print(f"একটি সমস্যা হয়েছে: {e}")