# Vulture whitelist for Protocol methods and context manager parameters
# These are part of Python's typing/context manager protocol and must match exact signatures

# Protocol method parameters (used by implementations, not directly in protocol definition)
system  # LLMClient.complete() system parameter - used by all implementations
exc_type  # __exit__ first parameter - Python context manager protocol
tb  # __exit__ third parameter - Python context manager protocol
