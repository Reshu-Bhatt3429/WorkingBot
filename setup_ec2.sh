#!/bin/bash

# Exit on error
set -e

echo "============================================="
echo " Polymarket Bot EC2 Setup Script"
echo "============================================="

# 1. Update system and install required system packages
echo "[*] Updating system packages..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-venv git tmux htop

# 2. Check if we are in the project directory
if [ ! -f "requirements.txt" ]; then
    echo "[!] Error: requirements.txt not found!"
    echo "Please run this script from inside the bot's project directory."
    echo "Example: "
    echo "  git clone https://github.com/Reshu-Bhatt3429/WorkingBot.git"
    echo "  cd WorkingBot"
    echo "  bash setup_ec2.sh"
    exit 1
fi

# 3. Setup Python Virtual Environment
echo "[*] Setting up Python virtual environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "Virtual environment 'venv' created."
else
    echo "Virtual environment 'venv' already exists."
fi

# 4. Install Python dependencies
echo "[*] Installing Python dependencies..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 5. Environment Variables Setup (.env)
echo "[*] Configuring Environment Variables..."
if [ ! -f ".env" ]; then
    echo "No .env file found. Let's create one now."
    read -p "Enter your POLYMARKET_API_KEY: " polymarket_api_key
    read -p "Enter your POLYMARKET_PRIVATE_KEY: " polymarket_private_key
    read -p "Enter your POLYMARKET_API_SECRET: " polymarket_api_secret

    cat <<EOF > .env
POLYMARKET_API_KEY=$polymarket_api_key
POLYMARKET_PRIVATE_KEY=$polymarket_private_key
POLYMARKET_API_SECRET=$polymarket_api_secret
EOF
    echo ".env file created successfully!"
else
    echo ".env file already exists. Skipping creation."
fi

echo "============================================="
echo " Setup Complete! 🎉"
echo "============================================="
echo ""
echo "To run your bot, you can use tmux to keep it running in the background even after you disconnect from the EC2 instance:"
echo ""
echo "1. Start a new tmux session:"
echo "   tmux new -s polymarket_bot"
echo ""
echo "2. Activate the virtual environment:"
echo "   source venv/bin/activate"
echo ""
echo "3. Run the bot:"
echo "   python main.py"
echo ""
echo "4. Detach from tmux (leave it running in background):"
echo "   Press Ctrl+B, then press D"
echo ""
echo "To reattach to the session later, type: tmux attach -t polymarket_bot"
