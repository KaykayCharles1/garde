# Garde

A Telegram bot that watches tokens for risky onchain activity on Robinhood Chain and Solana.
It emits risk signals only. Not audited, not financial advice, and alerts can arrive after the damage.

## Run locally
1. `cp .env.example .env` and fill in your values
2. `pip install -e ".[dev]"`
3. `python -m garde.main`

Status: milestone M0 (scaffold). Architecture and threat model coming in later milestones.