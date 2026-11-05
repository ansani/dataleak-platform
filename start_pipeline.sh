#!/bin/bash

# Session name
SESSION="dataleak_pipeline"

# Check if tmux is installed
if ! command -v tmux &> /dev/null; then
    echo "Error: tmux is not installed. Please install it (sudo apt install tmux)."
    exit 1
fi

# Kill existing session if it exists
tmux kill-session -t $SESSION 2>/dev/null

# Start a new detached session
tmux new-session -d -s $SESSION -n 'download'

# Define the workers to launch
# Format: "window_name:script_path"
WORKERS=(
    "channels:channels_list.py"
    "download:download.py"
    "links:download_links.py"
    "gofile:download_gofile.py"
    "password:password.py"
    "decompress:decompress.py"
    "subdirs:process_subdirectories.py"
    "scaler:analyze_scaler.py"
    "mail:send_mail.py --loop"
    "subscribe:subscribe_channels.py"
)

echo "Launching DataLeak Pipeline Workers..."

# Launch the first worker in the first window
tmux send-keys -t $SESSION:0 ".venv/bin/python ${WORKERS[0]#*:}" C-m

# Launch the rest in new windows
for ((i=1; i<${#WORKERS[@]}; i++)); do
    window_name=${WORKERS[$i]%%:*}
    script_path=${WORKERS[$i]#*:}
    
    tmux new-window -t $SESSION -n "$window_name"
    tmux send-keys -t "$SESSION:$window_name" ".venv/bin/python $script_path" C-m
    echo "Started $window_name..."
done

echo "----------------------------------------------------------------"
echo "Pipeline started successfully in tmux session: $SESSION"
echo "To inspect the logs, run: tmux attach -t $SESSION"
echo "To switch windows, use: Ctrl+b then 'n' (next) or 'p' (previous)"
echo "To detach from session, use: Ctrl+b then 'd'"
echo "----------------------------------------------------------------"
echo "Note: send_mail.py is a batch script. Run it manually when needed:"
echo "  .venv/bin/python send_mail.py 0"
