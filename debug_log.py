"""Opt-in local diagnostics for the QQ delivery workflow."""
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger('return_app.qq')
logger.addHandler(logging.NullHandler())
if os.environ.get('PHOTO_RETURN_DEBUG') == '1':
    folder = (Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent) / 'logs'
    folder.mkdir(exist_ok=True)
    path = folder / ('qq-debug-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.log')
    formatter = logging.Formatter('%(asctime)s [%(threadName)s] %(levelname)s %(message)s')
    file_handler = logging.FileHandler(path, encoding='utf-8')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        logger.addHandler(console)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.info('DEBUG enabled; log=%s; python=%s; source=%s', path, sys.executable, Path(__file__).resolve().parent)
