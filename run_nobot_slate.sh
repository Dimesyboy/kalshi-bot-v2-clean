#!/bin/bash
# Nobot slate runner — fires NO combos pre-game for all tonight's games
# Target: 8 legs, $50, ~10c cost, ~$50 win, 500x payout
# Run: bash run_nobot_slate.sh
# Cron: 0 22 * * * /root/kalshi-bot-v2/run_nobot_slate.sh

cd /root/kalshi-bot-v2
source /root/kalshi-bot/bin/activate

LOG="/root/kalshi-bot-v2/logs/nobot_slate.log"
mkdir -p /root/kalshi-bot-v2/logs
echo "=== SLATE $(date) ===" >> $LOG

run_game() {
    local game=$1
    local target=${2:-50.00}
    local legs=${3:-8}
    echo "[$(date +%H:%M)] Firing $game target=\$$target legs=$legs" | tee -a $LOG
    python3 nobot.py $game $target $legs 2>&1 | grep -E "PLACED|yes_c|Payout|Balance|failed|legs" | tee -a $LOG
    echo "" >> $LOG
}

# 4pm PT tip (23:00 UTC) — fire at 3pm PT (22:00 UTC)
run_game PHIWAS 50.00 8
run_game ATLORL 50.00 8

sleep 60

# 4:30pm PT tip — fire now too
run_game BOSMIA 50.00 8

sleep 60

# 5pm PT tips
run_game NYKMEM 50.00 8
run_game SACKTOR 50.00 8
run_game INDCHI 50.00 8
run_game MILHOU 50.00 8

sleep 60

# 6pm PT tip
run_game DENUTА 50.00 8

sleep 60

# 7pm PT tip
run_game SASGSW 50.00 8

echo "=== DONE $(date) ===" | tee -a $LOG
