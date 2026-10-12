class Base:
    def greet(self):
        return "hello from Base"


class Child(Base):
    def greet(self):
        return super().greet() + " (via Child)"
