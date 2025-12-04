# utils/token_generator.py
# optional helper if you want token generation in Python rather than DB function
def format_token(prefix, suffix, date_str):
    return f"{prefix}-{str(suffix).zfill(3)}-{date_str}"
