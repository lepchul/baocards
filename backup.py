"""Safe SQLite backup, including committed WAL transactions."""
import argparse
import os
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from core import Store

if __name__ == '__main__':
    load_dotenv()
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',default='backups')
    args=parser.parse_args()
    directory=Path(args.output); directory.mkdir(parents=True,exist_ok=True)
    path=directory / ('bao-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'.sqlite3')
    Store(os.getenv('DATABASE_PATH','data/bao.sqlite3')).backup(path)
    print(path)
