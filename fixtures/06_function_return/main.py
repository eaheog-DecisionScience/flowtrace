class Session:
    def get(self, url):
        return url


def make_session():
    return Session()


def main():
    session = make_session()
    return session.get("http://example.com")  # NOT resolved by Phase 1 - function-return inference is Phase 2
