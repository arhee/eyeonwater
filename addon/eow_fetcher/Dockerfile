# Use a full Python base image directly. The Home Assistant base image ships
# no Python/pip, and the Supervisor ignored build.yaml's build_from, so we pin
# the base here instead. python:3.13-slim is multi-arch (incl. arm64) and has
# prebuilt manylinux wheels for aiohttp + pydantic-core, so no compiler needed.
FROM python:3.13-slim

RUN pip install --no-cache-dir "pyonwater==0.3.32" "aiohttp>=3.9"

COPY eow_fetch_push.py /eow_fetch_push.py

# The script loops on its own (interval_minutes); no init system needed.
CMD [ "python", "/eow_fetch_push.py" ]
