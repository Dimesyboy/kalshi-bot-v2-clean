#!/bin/bash
# Nobot slate runner — fires 10 NO combos per game @ $1 each
# Cron: */30 * * * * /root/kalshi-bot-v2/run_nobot_slate.sh

cd /root/kalshi-bot-v2
source /root/kalshi-bot/bin/activate

LOG="/root/kalshi-bot-v2/logs/nobot_slate.log"
FIRED_LOG="/root/kalshi-bot-v2/logs/nobot_fired_today.log"
mkdir -p /root/kalshi-bot-v2/logs
touch $FIRED_LOG

TARGET=${NOBOT_TARGET:-1.00}
LEGS=${NOBOT_LEGS:-8}
COMBOS_PER_GAME=10

# Get games tipping in 25-40 mins
GAMES=$(python3 -c "
import requests
from datetime import datetime, timezone, timedelta
headers = {'User-Agent': 'Mozilla/5.0'}
r = requests.get('https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard',
    headers=headers, timeout=6)
now = datetime.now(timezone.utc)
for e in r.json().get('events',[]):
    status = e.get('status',{}).get('type',{}).get('name','')
    if status != 'STATUS_SCHEDULED': continue
    tip = datetime.fromisoformat(e.get('date','').replace('Z','+00:00'))
    mins = (tip - now).total_seconds() / 60
    if 25 <= mins <= 40:
        teams = e.get('competitions',[{}])[0].get('competitors',[])
        abbrs = ''.join(t['team']['abbreviation'] for t in teams)
        print(abbrs)
" 2>/dev/null)

if [ -z "$GAMES" ]; then exit 0; fi

TODAY=$(date +%Y%m%d)

for GAME in $GAMES; do
    echo "=== $(date) FIRING $GAME x${COMBOS_PER_GAME} ===" >> $LOG

    for i in $(seq 1 $COMBOS_PER_GAME); do
        FIRE_KEY="${TODAY}_${GAME}_${i}"
        if grep -q "$FIRE_KEY" $FIRED_LOG; then
            continue
        fi

        echo "[$(date +%H:%M:%S)] $GAME combo #$i" | tee -a $LOG

        python3 -c "
import random
random.seed()  # different seed each run for optimizer diversity
from nobot import fire_no_combo
result = fire_no_combo(game_filter=None, target='$TARGET', label='${GAME}_${i}', n_legs=$LEGS)
print('SUCCESS' if result else 'FAILED')
" 2>&1 | grep -E "PLACED|REJECTED|Optimizer|Best combo|no_bid|SUCCESS|FAILED|Added UNDER" | tee -a $LOG

        echo "$FIRE_KEY" >> $FIRED_LOG
        sleep 5  # small gap between combos
    done

    echo "" >> $LOG
done
