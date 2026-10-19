from cryptography.fernet import Fernet

from app.security import SecretBox


def test_secret_roundtrip():
    box = SecretBox(Fernet.generate_key().decode("ascii"))
    encrypted = box.encrypt("sensitive session")
    assert encrypted != "sensitive session"
    assert box.decrypt(encrypted) == "sensitive session"
