class Session:
    @classmethod
    def default(cls):
        return cls()

    def get(self, url):
        return url


def main():
    return Session.default()
