"""V0.4 副作用基础设施：状态目录、intent 存储、快照、路径锁与执行服务

目录外一次性授权（grant）在此只提供数据模型与校验逻辑；目录外执行的
开放排在 V0.5.1（审批通道 + pending-action 边界）之后。
"""

from my_agent.side_effects.intent_store import (
    GrantRecord,
    IntentRecord,
    IntentStore,
    SnapshotRecord,
    validate_grant,
)
from my_agent.side_effects.path_locks import (
    PathLockManager,
    StaleFencingTokenError,
)
from my_agent.side_effects.service import (
    ReadFingerprint,
    RecoveryReport,
    SideEffectService,
    WriteOutcome,
    WriteRequest,
)
from my_agent.side_effects.snapshots import SnapshotStore
from my_agent.side_effects.state_dir import (
    StateDirectoryError,
    default_state_root,
    ensure_state_directory,
)

__all__ = [
    "GrantRecord",
    "IntentRecord",
    "IntentStore",
    "PathLockManager",
    "ReadFingerprint",
    "RecoveryReport",
    "SideEffectService",
    "SnapshotRecord",
    "SnapshotStore",
    "StaleFencingTokenError",
    "StateDirectoryError",
    "WriteOutcome",
    "WriteRequest",
    "default_state_root",
    "ensure_state_directory",
    "validate_grant",
]
