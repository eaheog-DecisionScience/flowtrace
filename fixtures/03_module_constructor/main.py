import lib


def main():
    session = lib.Session()
    return session.get("http://example.com")
