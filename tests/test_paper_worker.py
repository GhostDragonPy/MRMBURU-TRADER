from types import SimpleNamespace
from threading import Event
from unittest.mock import Mock
from apps.worker.main import paper_loop
from services.pipeline import simulator

def test_worker_retries_errors_without_logging_secrets(factory, monkeypatch):
    stop=Event(); cache=Mock(); calls=[]
    def cycle(*args, **kwargs):
        calls.append(1)
        if len(calls)==1: raise RuntimeError('secret-must-not-be-logged')
        stop.set()
        return {'events':[]}
    monkeypatch.setattr(simulator,'cycle',cycle)
    monkeypatch.setattr('services.ctrader.feed.feed_from_settings',lambda *a:object())
    monkeypatch.setattr(stop,'wait',lambda timeout: None)
    settings=SimpleNamespace(paper_account_id='test',paper_allow_unknown_news=False)
    paper_loop(settings,factory,cache,stop)
    assert len(calls)==2
    cache.set.assert_any_call('paper:last_error','RuntimeError')
    cache.delete.assert_called_with('paper:last_error')
