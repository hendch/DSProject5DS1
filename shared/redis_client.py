import os
import redis
from dotenv import load_dotenv

load_dotenv()

def get_redis():
    return redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=int(os.getenv("REDIS_PORT", "6379")))