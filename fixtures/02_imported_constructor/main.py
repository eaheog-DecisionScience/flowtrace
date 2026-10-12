from lib import Session


def main():
    session = Session()
    return session.get("http://example.com")
