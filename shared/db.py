import os
from dotenv import load_dotenv
import psycopg2

load_dotenv()

def get_connection(use_shared: bool = False):
    url = os.getenv("NEON_DATABASE_URL") if use_shared else os.getenv("DATABASE_URL")
    return psycopg2.connect(url)