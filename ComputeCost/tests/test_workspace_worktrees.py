"""Real local Git worktrees; no repository/network/production fixture access."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pulse_compute_cost.receipt_sources import _same_registered_workspace

class WorkspaceWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.repo=self.root/'repo';self.repo.mkdir()
        self.git(self.repo,'init','-q')
        (self.repo/'local-pulse-space').mkdir();(self.repo/'local-pulse-space/identity.txt').write_text('fixture')
        (self.repo/'other-space').mkdir();(self.repo/'other-space/identity.txt').write_text('other')
        self.git(self.repo,'add','.')
        self.git(self.repo,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','-c','commit.gpgsign=false','commit','-qm','fixture')
        self.checkout=self.root/'native-worktree'
        self.git(self.repo,'worktree','add','--detach',str(self.checkout),'HEAD')
        self.declared=str(self.repo/'local-pulse-space')
        self.actual=str(self.checkout/'local-pulse-space')
    def git(self,where,*arguments):
        result=subprocess.run(['git','-C',str(where),*arguments],capture_output=True,text=True,timeout=15)
        if result.returncode:raise RuntimeError(result.stderr)
        return result.stdout
    def test_registered_worktree_same_relative_workspace_matches(self):
        self.assertTrue(_same_registered_workspace(self.declared,self.actual))
    def test_same_worktree_different_workspace_does_not_match(self):
        self.assertFalse(_same_registered_workspace(self.declared,str(self.checkout/'other-space')))
    def test_same_name_in_an_independent_clone_does_not_match(self):
        clone=self.root/'independent'
        self.git(self.root,'clone','--quiet',str(self.repo),str(clone))
        self.assertFalse(_same_registered_workspace(self.declared,str(clone/'local-pulse-space')))
    def test_unregistered_ordinary_directory_cannot_claim_identity(self):
        fake=self.root/'fake/local-pulse-space';fake.mkdir(parents=True)
        self.assertFalse(_same_registered_workspace(self.declared,str(fake)))
    def test_worktree_root_itself_is_not_agent_workspace(self):
        self.assertFalse(_same_registered_workspace(str(self.repo),str(self.checkout)))
    def test_per_read_cache_avoids_subprocess_per_usage_event(self):
        cache={}
        self.assertTrue(_same_registered_workspace(self.declared,self.actual,cache))
        with patch('subprocess.run',side_effect=AssertionError('must use established read cache')):
            self.assertTrue(_same_registered_workspace(self.declared,self.actual,cache))
    def test_exact_registered_folder_still_needs_no_git_process(self):
        with patch('subprocess.run',side_effect=AssertionError('exact declaration')):
            self.assertTrue(_same_registered_workspace(self.declared,self.declared))

if __name__=='__main__':unittest.main()
