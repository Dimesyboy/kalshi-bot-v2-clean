#!/bin/bash
# MLB nobot slate runner — fires NO combos 30min before each game
# Cron: */30 * * * * /root/kalshi-bot-v2/run_mlbot_slate.sh

cd /root/kalshi-bot-v2
source /root/kalshi-bot/bin/activate

LOG="/root/kalshi-bot-v2/logs/mlbot_slate.log"
FIRED_LOG="/root/kalshi-bot-v2/logs/mlbot_fired_today.log"
mkdir -p /root/kalshi-bot-v2/logs

TARGET=${MLBOT_TARGET:-1.50}
LEGS=${MLBOT_LEGS:-8}

GAMES=$(python3 -c "
import requests
from datetime import datetime, timezone, timedelta
headers = {'User-Agent': 'Mozilla/5.0'}
r = requests.get('https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard',
    headers=headers, timeout=6)
now = datetime.now(timezone.utc)
for e in r.json().get('events',[]):
    status = e.get('status',{}).get('type',{}).get('name','')
    if status != 'STATUS_SCHEDULED': continue
    tip = datetime.fromisoformat(e.get('date','').replace('Z','+00:00'))
    mins = (tip - now).total_seconds() / 60
    if 25 <= mins <= 40:
        teams = e.get('competitions',[{}])[0].get('competitors',[])
        abbrs = [t['team']['abbreviation'] for t in teams]
        print(''.join(abbrs))
" 2>/dev/null)

if [ -z "\$GAMES" ]; then exit 0; fi

TODAY=\$(date +%Y%m%d)
touch \$FIRED_LOG

for GAME in \$GAMES; do
    FIRE_KEY="MLB_\${TODAY}_\${GAME}"
    if grep -q "\$FIRE_KEY" \$FIRED_LOG; then continue; fi

    echo "=== \$(date) MLB FIRING \$GAME ===" >> \$LOG
    python3 -c "
from mlbot import fire_no_combo
result = fire_no_combo(game_filter=None, target='$TARGET', label='MLB_$GAME', n_legs=$LEGS)
print('SUCCESS' if result else 'FAILED')
" 2>&1 | grep -E "PLACED|REJECTED|Preview|Sizing|FILL|SUCCESS|FAILED|no_bid|Added MLB" | tee -a \$LOG

    echo "\$FIRE_KEY" >> \$FIRED_LOG
done
