import pkg


def main():
    pkg.get("http://example.com")
    session = pkg.Session()
    return session.get("http://example.com")
