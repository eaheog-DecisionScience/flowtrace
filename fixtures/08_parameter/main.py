class Session:
    def get(self, url):
        return url


def handle(session):
    return session.get("http://example.com")  # NOT resolved - `session` is a parameter, no assignment to trace


def main():
    return handle(Session())
