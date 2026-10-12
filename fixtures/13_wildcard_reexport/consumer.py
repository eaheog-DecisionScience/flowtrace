import pkg


def main():
    client = pkg.Client()
    return client.get("http://example.com")
