import os


# Pydantic AI prints a banner on its first agent, which would land in the middle of a benchmark's output.
os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')
