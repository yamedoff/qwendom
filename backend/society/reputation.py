from __future__ import annotations

from dataclasses import dataclass, field


MIN_TRUST = 0.0
BASELINE_TRUST = 1.0
MAX_TRUST = 2.0
DECAY_RATE = 0.02
TASK_CLASSES = ("research", "planning", "implementation", "review")


def _clamp(value: float) -> float:
    """Clamp trust scores to a stable product range."""

    return max(MIN_TRUST, min(MAX_TRUST, value))


def _decay_toward_baseline(value: float) -> float:
    """Move a score slightly toward baseline without erasing history."""

    return _clamp(value + (BASELINE_TRUST - value) * DECAY_RATE)


@dataclass
class ReputationRecord:
    """Multi-dimensional reputation for governance decisions."""

    leadership: float = 1.0
    delivery: float = 1.0
    critique: float = 1.0
    collaboration: float = 1.0
    task_class_scores: dict[str, dict[str, float]] = field(default_factory=dict)
    social_signals: dict[str, int] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)

    @property
    def election_score(self) -> float:
        """Weighted score used when selecting a task leader."""

        return (
            self.leadership * 0.35
            + self.delivery * 0.30
            + self.critique * 0.15
            + self.collaboration * 0.20
        )

    def contextual_election_score(self, task_class: str | None = None) -> float:
        """Weighted election score with task-class trust blended in."""

        if not task_class:
            return self.election_score
        class_scores = self.task_class_scores.get(task_class, {})
        if not class_scores:
            return self.election_score
        task_score = (
            class_scores.get("leadership", BASELINE_TRUST) * 0.35
            + class_scores.get("delivery", BASELINE_TRUST) * 0.30
            + class_scores.get("critique", BASELINE_TRUST) * 0.15
            + class_scores.get("collaboration", BASELINE_TRUST) * 0.20
        )
        return self.election_score * 0.65 + task_score * 0.35

    def _task_scores(self, task_class: str) -> dict[str, float]:
        return self.task_class_scores.setdefault(
            task_class,
            {
                "leadership": BASELINE_TRUST,
                "delivery": BASELINE_TRUST,
                "critique": BASELINE_TRUST,
                "collaboration": BASELINE_TRUST,
            },
        )

    def _decay_all(self) -> None:
        self.leadership = _decay_toward_baseline(self.leadership)
        self.delivery = _decay_toward_baseline(self.delivery)
        self.critique = _decay_toward_baseline(self.critique)
        self.collaboration = _decay_toward_baseline(self.collaboration)
        for scores in self.task_class_scores.values():
            for key, value in list(scores.items()):
                scores[key] = _decay_toward_baseline(float(value))

    def _signal(self, key: str, amount: int = 1) -> None:
        self.social_signals[key] = int(self.social_signals.get(key, 0)) + amount

    def apply_task_result(
        self,
        agent_id: str,
        winner_id: str,
        role: str,
        evaluation_metrics: list[dict] | None = None,
        task_class: str | None = None,
        social_updates: list[dict] | None = None,
    ) -> dict[str, float]:
        """Update contextual trust after a task and return the applied deltas."""

        won = agent_id == winner_id
        task_class = task_class if task_class in TASK_CLASSES else "planning"
        metrics = evaluation_metrics or []
        updates = social_updates or []
        confidence = next((float(m.get("value", 0.0)) for m in metrics if m.get("metric_name") == "confidence"), 0.0)
        blockers = next((float(m.get("value", 0.0)) for m in metrics if m.get("metric_name") == "blocker_severity"), 0.0)
        self._decay_all()

        deltas = {"leadership": 0.0, "delivery": 0.0, "critique": 0.0, "collaboration": 0.0}
        validation_passed = next((float(m.get("value", 0.0)) for m in metrics if m.get("metric_name") == "validation_pass"), 1.0)
        deltas["collaboration"] += 0.01
        if won:
            deltas["delivery"] += 0.08
        elif validation_passed >= 1.0 and blockers == 0:
            deltas["collaboration"] += 0.01
        if won and confidence >= 0.75 and blockers == 0 and validation_passed >= 1.0:
            deltas["delivery"] += 0.03
        if "reviewer" in role.lower() or "critic" in role.lower():
            deltas["critique"] += 0.03
        if confidence >= 0.75 and ("reviewer" in role.lower() or "critic" in role.lower()):
            deltas["critique"] += 0.02
        if won:
            deltas["leadership"] += 0.05
            self._signal("wins")
        if blockers > 0 and won:
            deltas["delivery"] -= 0.06
            self._signal("blocked_wins")
        if validation_passed <= 0:
            deltas["delivery"] -= 0.04 if won else 0.01
            deltas["collaboration"] -= 0.01
            self._signal("validation_failures")

        for update in updates:
            domain = str(update.get("domain", "collaboration"))
            delta = float(update.get("delta", 0.0))
            target = update.get("target_agent_id")
            if target != agent_id:
                continue
            if domain in deltas:
                deltas[domain] += delta
            elif domain == "task leadership":
                deltas["leadership"] += delta
            elif domain == "endorsement":
                deltas["leadership"] += delta
            elif domain == "evidence":
                deltas["critique"] += delta
            else:
                deltas["collaboration"] += delta
            if delta >= 0:
                self._signal("positive_social_updates")
            else:
                self._signal("negative_social_updates")

        task_scores = self._task_scores(task_class)
        for key, delta in deltas.items():
            setattr(self, key, _clamp(float(getattr(self, key)) + delta))
            task_scores[key] = _clamp(float(task_scores.get(key, BASELINE_TRUST)) + delta)
        self.history.append({
            "winner": won,
            "role": role,
            "task_class": task_class,
            "confidence": confidence,
            "blockers": blockers,
            "deltas": {key: round(value, 3) for key, value in deltas.items() if value},
        })
        return deltas


class ReputationStore:
    """In-process reputation store backed by JSON-serializable snapshots."""

    def __init__(self) -> None:
        self._records: dict[str, ReputationRecord] = {}

    def ensure(self, agent_id: str) -> ReputationRecord:
        return self._records.setdefault(agent_id, ReputationRecord())

    def election_score(self, agent_id: str, fallback: float = 1.0) -> float:
        record = self._records.get(agent_id)
        return record.election_score if record else fallback

    def contextual_election_score(self, agent_id: str, task_class: str | None = None, fallback: float = 1.0) -> float:
        record = self._records.get(agent_id)
        return record.contextual_election_score(task_class) if record else fallback

    def apply_task_result(
        self,
        agent_id: str,
        winner_id: str,
        role: str,
        evaluation_metrics: list[dict] | None = None,
        task_class: str | None = None,
        social_updates: list[dict] | None = None,
    ) -> dict[str, float]:
        return self.ensure(agent_id).apply_task_result(agent_id, winner_id, role, evaluation_metrics, task_class, social_updates)

    def snapshot(self) -> dict[str, dict]:
        return {
            agent_id: {
                "leadership": record.leadership,
                "delivery": record.delivery,
                "critique": record.critique,
                "collaboration": record.collaboration,
                "election_score": record.election_score,
                "task_class_scores": record.task_class_scores,
                "social_signals": record.social_signals,
                "history": record.history[-10:],
            }
            for agent_id, record in self._records.items()
        }

    def load_snapshot(self, snapshot: dict[str, dict]) -> None:
        """Restore reputation records from a persisted JSON snapshot."""

        for agent_id, raw in snapshot.items():
            record = self.ensure(agent_id)
            fallback_score = raw.get("legacy_score", raw.get("election_score", BASELINE_TRUST))
            try:
                fallback_float = _clamp(float(fallback_score))
            except (TypeError, ValueError):
                fallback_float = BASELINE_TRUST
            record.leadership = _clamp(float(raw.get("leadership", fallback_float)))
            record.delivery = _clamp(float(raw.get("delivery", fallback_float)))
            record.critique = _clamp(float(raw.get("critique", fallback_float)))
            record.collaboration = _clamp(float(raw.get("collaboration", fallback_float)))
            raw_task_scores = raw.get("task_class_scores", {})
            if isinstance(raw_task_scores, dict):
                restored_scores: dict[str, dict[str, float]] = {}
                for task_class, scores in raw_task_scores.items():
                    if not isinstance(scores, dict):
                        continue
                    restored_scores[str(task_class)] = {
                        str(key): _clamp(float(value))
                        for key, value in scores.items()
                    }
                record.task_class_scores = restored_scores
            raw_signals = raw.get("social_signals", {})
            if isinstance(raw_signals, dict):
                record.social_signals = {str(key): int(value) for key, value in raw_signals.items()}
            history = raw.get("history", [])
            record.history = history if isinstance(history, list) else []
