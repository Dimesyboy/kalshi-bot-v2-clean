#!/bin/bash
# Nobot slate runner — fires NO combos ~30min before each game tip
# Cron fires every 30min, script checks which games tip soon
# Cron: */30 * * * * /root/kalshi-bot-v2/run_nobot_slate.sh

cd /root/kalshi-bot-v2
source /root/kalshi-bot/bin/activate

LOG="/root/kalshi-bot-v2/logs/nobot_slate.log"
FIRED_LOG="/root/kalshi-bot-v2/logs/nobot_fired_today.log"
mkdir -p /root/kalshi-bot-v2/logs

TARGET=${NOBOT_TARGET:-1.50}
LEGS=${NOBOT_LEGS:-8}

# Get today's pre-game games and their tip times
GAMES=$(python3 -c "
import requests, json
from datetime import datetime, timezone, timedelta
headers = {'User-Agent': 'Mozilla/5.0'}
r = requests.get('https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard',
    headers=headers, timeout=6)
now = datetime.now(timezone.utc)
for e in r.json().get('events',[]):
    status = e.get('status',{}).get('type',{}).get('name','')
    if status != 'STATUS_SCHEDULED': continue
    tip = datetime.fromisoformat(e.get('date','').replace('Z','+00:00'))
    mins_to_tip = (tip - now).total_seconds() / 60
    # Fire window: 25-40 min before tip
    if 25 <= mins_to_tip <= 40:
        teams = e.get('competitions',[{}])[0].get('competitors',[])
        abbrs = [t['team']['abbreviation'] for t in teams]
        game_code = ''.join(abbrs)
        print(game_code)
" 2>/dev/null)

if [ -z "\$GAMES" ]; then
    exit 0
fi

# Check if already fired today for this game
TODAY=\$(date +%Y%m%d)
touch \$FIRED_LOG

for GAME in \$GAMES; do
    FIRE_KEY="\${TODAY}_\${GAME}"
    if grep -q "\$FIRE_KEY" \$FIRED_LOG; then
        continue  # already fired today
    fi

    echo "=== \$(date) FIRING \$GAME ===" >> \$LOG
    echo "[\$(date +%H:%M)] Firing \$GAME target=\$TARGET legs=\$LEGS" | tee -a \$LOG

    python3 -c "
from nobot import fire_no_combo
result = fire_no_combo(game_filter=None, target='$TARGET', label='$GAME', n_legs=$LEGS)
print('SUCCESS' if result else 'FAILED')
" 2>&1 | grep -E "PLACED|REJECTED|Preview|Sizing|FILL|SUCCESS|FAILED|no_bid|Balance|Added UNDER" | tee -a \$LOG

    # Mark as fired
    echo "\$FIRE_KEY" >> \$FIRED_LOG
    echo "" >> \$LOG
done
