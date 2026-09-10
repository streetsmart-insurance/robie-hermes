"""
Quoted Prospects Workflow (Days 1, 3, 7 post-quote).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from src.models.cadence_models import CadenceType, ChannelType, Opportunity
from src.workflows.base_workflow import BaseWorkflow


class QuotedProspectWorkflow(BaseWorkflow):
    def __init__(self, **kwargs):
        super().__init__(cadence_type=CadenceType.QUOTED_PROSPECT, **kwargs)

    def calculate_first_touch_due(self, now: datetime, opportunity: Opportunity) -> datetime:
        # Touch 1 fires on Day 1 (24 hours post-quote)
        return now + timedelta(days=1)

    def calculate_next_touch_due(
        self, completed_touch: int, now: datetime, opportunity: Opportunity
    ) -> Optional[datetime]:
        if completed_touch == 1:
            # Move to Touch 2 (Day 3): 2 days later
            return now + timedelta(days=2)
        elif completed_touch == 2:
            # Move to Touch 3 (Day 7): 4 days later
            return now + timedelta(days=4)
        return None  # Sequence completed

    def get_touch_channels(self, touch_number: int) -> List[ChannelType]:
        if touch_number == 0:
            # Touch 1 in Jake's specification corresponds to index 0/1
            return [ChannelType.VOICE, ChannelType.EMAIL]
        elif touch_number == 1:
            return [ChannelType.EMAIL, ChannelType.SMS]
        elif touch_number == 2:
            return [ChannelType.VOICE, ChannelType.EMAIL]
        return []
