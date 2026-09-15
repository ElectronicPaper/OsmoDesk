"""Correlated ACK regressions; native Claude Sonnet draft corrected/reviewed.

No sockets are opened. ACK proves command acceptance, never a saved file.
"""
import threading
import time
import unittest
from types import SimpleNamespace
from driver import datalink, duml, commands

class FakeSock:
    def __init__(self): self.sent = []
    def send(self, data): self.sent.append(data)
    def close(self): pass

def make_link():
    link = datalink.Datalink()
    link.sock = FakeSock()
    link.last_rx_at = time.monotonic()
    return link

def reply_for(request, payload=b'\0', **over):
    args = dict(sender=duml.RX_CAMERA, receiver=duml.SENDER_APP,
                seq=request.seq, flags=duml.FLAG_RESPONSE, cmd_set=2, cmd_id=1, payload=payload)
    args.update(over)
    return duml.Frame(**args)

class TestCameraRequests(unittest.TestCase):
    def test_success_before_wait(self):
        link = make_link(); req = commands.photo(); receipt = link.begin_request(req)
        link._handle(reply_for(req))
        self.assertEqual(receipt.wait().seq, req.seq)
        self.assertEqual(len(link.sock.sent), 1)

    def test_rejection_and_empty_are_not_success(self):
        for payload, message in ((b'\xd9', '0xD9'), (b'', 'unknown')):
            link = make_link(); req = commands.photo(); receipt = link.begin_request(req)
            link._handle(reply_for(req, payload))
            with self.assertRaisesRegex(RuntimeError, message): receipt.wait()
            self.assertEqual(len(link.sock.sent), 1)

    def test_unrelated_frames_cannot_satisfy_request(self):
        for change in ({'seq':123}, {'cmd_set':3}, {'cmd_id':9}, {'flags':0}, {'sender':3}, {'receiver':4}):
            link = make_link(); req = commands.photo(); receipt = link.begin_request(req)
            link._handle(reply_for(req, **change))
            with self.assertRaises(TimeoutError): receipt.wait(timeout=.001)
            self.assertEqual(len(link.sock.sent), 1)

    def test_cancel_close_and_loss_invalidate_without_retry(self):
        for change in ('cancel', 'close', 'loss'):
            link = make_link(); req = commands.photo(); receipt = link.begin_request(req)
            sock = link.sock; cancel = threading.Event()
            if change == 'cancel': cancel.set()
            elif change == 'close': link.close()
            else: link.last_rx_at = 0
            with self.assertRaises(RuntimeError): receipt.wait(cancel=cancel)
            self.assertEqual(len(sock.sent), 1)

    def test_late_old_ack_cannot_satisfy_new_receipt(self):
        link = make_link(); first = commands.photo(); old = link.begin_request(first)
        with self.assertRaises(TimeoutError): old.wait(timeout=.001)
        second = commands.photo(); new = link.begin_request(second)
        self.assertNotEqual(first.seq, second.seq)
        link._handle(reply_for(first))
        with self.assertRaises(TimeoutError): new.wait(timeout=.001)
        self.assertEqual(len(link.sock.sent), 2)

    def test_request_after_shutdown_cancellation_never_reaches_socket(self):
        link = make_link()
        sock = link.sock
        receipt = link.begin_request(commands.photo())
        def during_join(timeout):
            self.assertIs(link.sock, sock)
            self.assertEqual(link._pending_replies, {})
            with self.assertRaisesRegex(RuntimeError, 'closing'):
                link.begin_request(commands.photo())
        link._threads = [SimpleNamespace(join=during_join)]
        link.close()
        with self.assertRaises(RuntimeError): receipt.wait()
        self.assertEqual(len(sock.sent), 1)
        self.assertEqual(link._pending_replies, {})

    def test_pending_requests_are_bounded(self):
        link = make_link()
        receipts = [link.begin_request(commands.photo()) for _ in range(8)]
        with self.assertRaisesRegex(RuntimeError, 'too many'): link.begin_request(commands.photo())
        link.close()
        for receipt in receipts:
            with self.assertRaises(RuntimeError): receipt.wait()

    def test_send_failure_cannot_become_ack_success(self):
        link = make_link()
        def fail(_): raise OSError('failed')
        link.sock.send = fail
        with self.assertRaisesRegex(RuntimeError, 'send failed'): link.begin_request(commands.photo())
