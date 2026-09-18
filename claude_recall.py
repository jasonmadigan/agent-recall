#!/usr/bin/env python3
"""Compatibility entry point for installations made before Agent Recall."""
import sys
import agent_recall

if __name__ == "__main__":
    sys.exit(agent_recall.main())
else:
    sys.modules[__name__] = agent_recall
