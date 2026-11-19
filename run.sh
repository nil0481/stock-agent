#!/usr/bin/env bash
cd "$(dirname "$0")"
mkdir -p data
if [ "$1" = "stop" ] || [ "$1" = "restart" ]; then
  pkill -f "[r]adar/server.py" && echo "Stopped." ; sleep 1
  [ "$1" = "stop" ] && exit 0
fi
if ss -ltn | grep -q ':8379 '; then echo "Already running: http://localhost:8379"; exit 0; fi
setsid nohup python3 radar/server.py >> data/server.log 2>&1 < /dev/null &
sleep 1; echo "Started: http://localhost:8379  (log: data/server.log)"
