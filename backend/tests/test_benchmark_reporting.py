from __future__ import annotations
import sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from benchmarks import EvaluationResult
from benchmarks.reporting import aggregate_trials,compare_modes,write_bundle
from benchmarks.runtime import TrialResult,TrialUsage

def _trial(mode:str,score:float)->TrialResult:
    return TrialResult(mode=mode,status="success",wall_duration_s=60,evaluation=EvaluationResult(mode=mode,total_score=score),usage=TrialUsage(input_tokens=50,output_tokens=50,total_tokens=100,cost=.1,usage_complete=True))

class ReportingTests(unittest.TestCase):
    def test_failures_preserved(self): self.assertEqual(aggregate_trials([_trial("single_agent",80),TrialResult(error="x")])["failures"],1)
    def test_incomplete_usage_suppresses_efficiency(self):
        t=_trial("single_agent",80); t.usage.usage_complete=False; self.assertIsNone(aggregate_trials([t])["score_per_million_tokens"])
    def test_missing_cost_is_not_reported_as_free(self):
        t=_trial("single_agent",80); t.usage.cost=None; result=aggregate_trials([t]); self.assertIsNone(result["total_cost"]); self.assertFalse(result["cost_complete"])
    def test_one_trial_each_is_preliminary(self): self.assertEqual(compare_modes([_trial("single_agent",80)],[_trial("society",90)])["verdict"],"insufficient_data")
    def test_honest_comparison(self): self.assertEqual(compare_modes([_trial("single_agent",80)]*3,[_trial("society",90)]*3)["verdict"],"society_quality_win")
    def test_bundle_keeps_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=write_bundle(Path(tmp),[TrialResult(error="failed")],[]); self.assertTrue((out/"single_agent"/"trial-01.json").exists()); self.assertTrue((out/"comparison.json").exists())

if __name__=="__main__": unittest.main()
