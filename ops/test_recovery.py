"""Failure-path checks for network adoption and encrypted backup integrity."""
import hashlib
from io import BytesIO
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from ops import backup, backup_store, tailnet


class TailnetRecoveryTests(unittest.TestCase):
    def stop(self):
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        return stop

    def test_adopted_daemon_survives_supervisor_stop(self):
        with patch.object(tailnet, 'available', return_value=True), \
                patch.object(tailnet, 'notify_ready') as ready, \
                patch.object(tailnet.subprocess, 'Popen') as launch:
            tailnet.supervise({}, self.stop())
        ready.assert_called_once()
        launch.assert_not_called()

    def test_temporarily_unready_live_daemon_is_not_duplicated(self):
        with patch.object(tailnet, 'available', return_value=False), \
                patch.object(tailnet, 'daemon_running', return_value=True), \
                patch.object(tailnet.time, 'monotonic', return_value=10), \
                patch.object(tailnet.subprocess, 'Popen') as launch:
            tailnet.supervise({}, self.stop())
        launch.assert_not_called()

    def test_missing_daemon_restores_existing_identity_and_cleans_owned_child(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)/'state'
            state.write_text('existing-identity')
            config = dict(state_file=str(state), socket_file=str(state)+'.sock',
                          daemon='/packaged/tailscaled', socks_port=1055, http_port=1056)
            child = Mock()
            child.poll.return_value = None
            with patch.object(tailnet, 'available', return_value=False), \
                    patch.object(tailnet, 'daemon_running', return_value=False), \
                    patch.object(tailnet.time, 'monotonic', return_value=10), \
                    patch.object(tailnet.subprocess, 'Popen', return_value=child) as launch:
                tailnet.supervise(config, self.stop())
            self.assertIn('--state='+str(state), launch.call_args.args[0])
            child.terminate.assert_called_once()
            self.assertEqual(state.read_text(), 'existing-identity')


class BackupIntegrityTests(unittest.TestCase):
    def store(self, data):
        store = Mock()
        store.head_object.return_value = {'ContentLength': len(data)}
        store.get_object.return_value = {'Body': BytesIO(data)}
        return store

    def test_cloud_verification_reads_full_object(self):
        data = b'encrypted archive fixture'
        store = self.store(data)
        backup_store.verify_object(store, 'bucket', 'key', hashlib.sha256(data).hexdigest(), len(data))
        store.get_object.assert_called_once()

    def test_same_size_corrupt_cloud_object_is_rejected(self):
        expected = b'original ciphertext'
        with self.assertRaisesRegex(RuntimeError, 'checksum'):
            backup_store.verify_object(self.store(b'x'*len(expected)), 'bucket', 'key',
                                       hashlib.sha256(expected).hexdigest(), len(expected))

    def test_plaintext_cannot_be_published(self):
        with self.assertRaisesRegex(ValueError, 'encrypted'):
            backup_store.publish('database.dump')

    def test_external_wal_database_is_snapshotted_without_journals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'live'
            source.mkdir()
            with sqlite3.connect(source/'ledger.sqlite3') as writer:
                writer.execute('PRAGMA journal_mode=WAL')
                writer.execute('CREATE TABLE codes (value TEXT)')
                writer.execute("INSERT INTO codes VALUES ('inventory')")
                writer.commit()
                output = root/'snapshot.tar'
                databases = []
                with tarfile.open(output, 'w') as archive:
                    backup.add_external(archive, source, 'external/renewal', root/'stage', databases)
                    for path, name in databases:
                        archive.add(path, arcname=name)
                self.assertEqual(len(databases), 1)
                with sqlite3.connect(databases[0][0]) as restored:
                    self.assertEqual(restored.execute('SELECT value FROM codes').fetchone()[0], 'inventory')
                with tarfile.open(output) as archive:
                    self.assertFalse(any(n.endswith(('-wal', '-shm')) for n in archive.getnames()))


if __name__ == '__main__':
    unittest.main()
