#!/bin/bash
# Nobot slate runner — fires NO combos pre-game for all tonight's games
# Target: 3 legs, $1.50 risk, ~$4-6 win, 3-4x payout, EXTENDED collection
# Cron: 0 22 * * * /root/kalshi-bot-v2/run_nobot_slate.sh

cd /root/kalshi-bot-v2
source /root/kalshi-bot/bin/activate

LOG="/root/kalshi-bot-v2/logs/nobot_slate.log"
mkdir -p /root/kalshi-bot-v2/logs
echo "=== SLATE $(date) ===" >> $LOG

run_game() {
    local game=$1
    local target=${2:-1.50}
    local legs=${3:-3}
    echo "[$(date +%H:%M)] Firing $game target=\$$target legs=$legs" | tee -a $LOG
    python3 -c "
import sys
sys.path.insert(0, '/root/kalshi-bot-v2')
from nobot import fire_no_combo
result = fire_no_combo(game_filter='$game', target='$target', label='$game', n_legs=$legs)
print('SUCCESS' if result else 'FAILED')
" 2>&1 | grep -E "PLACED|REJECTED|Preview|Sizing|FILL|SUCCESS|FAILED|no_bid|Balance" | tee -a $LOG
    echo "" >> $LOG
}

# 4pm PT tip — fire at 3pm PT (22:00 UTC)
run_game PHIWAS 1.50 8
run_game ATLORL 1.50 8
run_game BOSMIA 1.50 8

# 5pm PT tips
run_game NYKMEM 1.50 8
run_game SACKTOR 1.50 8
run_game INDCHI 1.50 8
run_game MILHOU 1.50 8

# 6pm PT tip
run_game DENUTА 1.50 8

# 7pm PT tip
run_game SASGSW 1.50 8

echo "=== DONE $(date) ===" | tee -a $LOG
