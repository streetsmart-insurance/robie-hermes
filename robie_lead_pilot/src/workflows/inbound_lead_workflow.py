"""
New Inbound & Unreached Leads Workflow (Immediate Ack, Days 1, 3, 7).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from src.models.cadence_models import CadenceType, ChannelType, Opportunity
from src.workflows.base_workflow import BaseWorkflow


class InboundLeadWorkflow(BaseWorkflow):
    def __init__(self, **kwargs):
        super().__init__(cadence_type=CadenceType.INBOUND_LEAD, **kwargs)

    def calculate_first_touch_due(self, now: datetime, opportunity: Opportunity) -> datetime:
        # Touch 0 is immediate acknowledgment (2 minutes delay)
        return now + timedelta(minutes=2)

    def calculate_next_touch_due(
        self, completed_touch: int, now: datetime, opportunity: Opportunity
    ) -> Optional[datetime]:
        if completed_touch == 1:
            # Move from Touch 0 (immediate ack) to Touch 1 (Day 1 call): 24h
            return now + timedelta(days=1)
        elif completed_touch == 2:
            # Move from Touch 1 to Touch 2 (Day 3 check-in): 2 days
            return now + timedelta(days=2)
        elif completed_touch == 3:
            # Move from Touch 2 to Touch 3 (Day 7 final follow-up): 4 days
            return now + timedelta(days=4)
        return None  # All touches completed

    def get_touch_channels(self, touch_number: int) -> List[ChannelType]:
        if touch_number == 0:
            return [ChannelType.SMS, ChannelType.EMAIL]
        elif touch_number == 1:
            return [ChannelType.VOICE, ChannelType.EMAIL]
        elif touch_number == 2:
            return [ChannelType.EMAIL, ChannelType.SMS]
        elif touch_number == 3:
            return [ChannelType.VOICE, ChannelType.EMAIL]
        return []
