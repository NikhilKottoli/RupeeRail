// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Ownable2Step} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {IERC20Permit} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Permit.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {SignatureChecker} from "@openzeppelin/contracts/utils/cryptography/SignatureChecker.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";

/**
 * @title ScanPayVault
 * @notice Single-token escrow, held settlement funds and free payout pool; one vault per chain.
 * @dev Uses only ERC-20 calls, including on Arc where the token is also the gas coin.
 * For a non-rebasing, exact-transfer token, successful transaction boundaries satisfy:
 * token.balanceOf(address(this)) >= escrowedTotal + heldTotal + poolBalance.
 * The user and quote signer sign the same EIP-712 intent. The quote signer authenticates
 * inrPaise/rateE6 and binds the id to that user and quote, preventing id squatting.
 * qrCommitment is keccak256(abi.encode(salt, qrText)), computed off-chain; neither the
 * salt nor payee text is submitted to the vault. Use an unpredictable secret salt.
 * At lock, completion/reclaim deadlines and the dispute duration are fixed. The settler
 * can complete only at/after completableAt and strictly before reclaimableAt. Completion
 * holds funds through a dispute window, rather than immediately adding them to the pool.
 * Held funds can go only to the original user (reversal or dispute refund), or to the
 * pool (permissionless release after the window, or arbiter resolution). They never go
 * directly to the settler. A user dispute freezes release and snapshots a resolution deadline.
 * Before that deadline only the arbiter can refund the user or release disputed funds to the pool;
 * at/after it anyone can return the held funds to the original user via claimDisputeTimeout.
 * The chain cannot prove a fiat payment: the settler reports it and the arbiter adjudicates.
 * The settler can spend free pool funds through unique onramp orders within a fixed-window
 * cap. The owner manages roles/configuration, withdraws only free pool funds and sweeps
 * only unaccounted surplus; ownership uses two-step transfer and cannot be renounced.
 * The guardian can only pause. Pausing blocks new offramps and onramps, not settlement,
 * recovery or treasury operations. Only the owner can unpause.
 * Trust boundary: the owner is the root of trust. It can replace the settler, arbiter and quote
 * signer immediately, so it should be a multisig or other protected key kept separate from the
 * settler. Disputes time out: if the arbiter does not resolve before the snapshotted deadline,
 * anyone can return the held funds to the original user. The arbiter cannot release disputed
 * funds to the pool at/after that deadline; later timeout configuration changes do not extend it.
 * Direct token transfers are surplus, not pool funding. Fee-on-transfer deposits are
 * rejected; rebasing or externally debitable tokens are unsupported. Token blocklists
 * may prevent refunds: a blocklisted user's own escrow, reversal or dispute refund can fail
 * to transfer and stays in its status until the token unblocks the user (other users are
 * unaffected). The dispute duration cannot be set below MIN_DISPUTE_WINDOW, but it is not
 * part of the signed intent, so users get at least that floor. Role changes affect
 * outstanding intents and settlements.
 */
contract ScanPayVault is Ownable2Step, EIP712, ReentrancyGuard, Pausable {
    using SafeERC20 for IERC20;

    struct OfframpIntent {
        bytes32 id;
        address user;
        uint256 amount;
        uint64 inrPaise;
        uint64 rateE6;
        bytes32 qrCommitment;
        uint64 deadline;
    }

    struct PermitSig {
        uint256 deadline;
        uint8 v;
        bytes32 r;
        bytes32 s;
    }

    enum Status { NONE, LOCKED, COMPLETED, REFUNDED, REVERSED, RELEASED, DISPUTED }

    /// @dev One storage slot; first three fields fixed at lock, releasableAt set at completion.
    /// On dispute, releasableAt is overwritten with the snapshotted resolve deadline:
    /// deadlines(id).releasableAt for a DISPUTED offramp returns its resolve deadline.
    struct Deadlines {
        uint64 completableAt;
        uint64 reclaimableAt;
        uint64 disputeWindow;
        uint64 releasableAt;
    }

    struct Offramp {
        address user;
        uint96 amount;
        uint64 inrPaise;
        uint64 lockedAt;
        uint64 completedAt;
        Status status;
        bytes32 qrCommitment;
    }

    bytes32 public constant OFFRAMP_INTENT_TYPEHASH = keccak256(
        "OfframpIntent(bytes32 id,address user,uint256 amount,uint64 inrPaise,uint64 rateE6,bytes32 qrCommitment,uint64 deadline)"
    );
    /// @notice Smallest dispute window the owner may configure.
    uint256 public constant MIN_DISPUTE_WINDOW = 1 hours;
    /// @notice Smallest resolution timeout the owner may configure.
    uint256 public constant MIN_DISPUTE_TIMEOUT = 1 days;
    /// @notice Largest resolution timeout the owner may configure.
    uint256 public constant MAX_DISPUTE_TIMEOUT = 30 days;
    uint8 public constant REASON_INVALID_QR = 1;
    uint8 public constant REASON_AMOUNT_MISMATCH = 2;
    uint8 public constant REASON_DISPATCH_FAILED = 3;
    uint8 public constant REASON_OPERATOR = 4;
    uint8 public constant REASON_USER_RECLAIM = 5;

    IERC20 public immutable token;
    uint8 public immutable decimals;
    address public settler;
    address public quoteSigner;
    address public arbiter;
    address public guardian;
    mapping(bytes32 => Offramp) public offramps;
    mapping(bytes32 => Deadlines) public deadlines;
    mapping(bytes32 => bool) public onrampDone;
    mapping(bytes32 => bool) public payoutRefUsed;
    uint256 public escrowedTotal;
    uint256 public heldTotal;
    uint256 public poolBalance;

    uint256 public completeDelay = 15 seconds;
    uint256 public disputeWindow = 24 hours;
    /// @notice Resolution timeout snapshotted when an offramp is disputed.
    uint256 public disputeTimeout = 7 days;
    uint256 public reclaimDelay = 24 hours;
    uint256 public maxOfframpAmount;
    uint256 public dailyEmitCap;
    /// @notice Start of the current fixed 24-hour onramp window, initially deployment time.
    uint256 public emitWindowStart;
    uint256 public emittedInWindow;

    error ZeroAddress();
    error UnauthorizedSettler(address caller);
    error UnauthorizedUser(address caller);
    error UnauthorizedArbiter(address caller);
    error UnauthorizedGuardian(address caller);
    error OwnershipRenunciationDisabled();
    error UnsupportedDecimals(uint8 tokenDecimals);
    error TimestampOutOfRange();
    error IntentExpired(uint64 deadline);
    error OfframpIdUsed(bytes32 id);
    error InvalidQuote();
    error InvalidAmount(uint256 amount);
    error OfframpAmountTooLarge(uint256 amount);
    error InvalidSignature();
    error InsufficientAllowance(uint256 available, uint256 required);
    error ReceivedAmountMismatch(uint256 expected, uint256 balanceBefore, uint256 balanceAfter);
    error InvalidStatus(bytes32 id, Status actual, Status expected);
    error TooEarly(uint256 availableAt);
    error CompletionWindowClosed(uint256 closedAt);
    error DisputeWindowExpired(uint256 expiredAt);
    error DisputeResolutionExpired(uint256 deadline);
    error InvalidDisputeTimeout(uint256 value);
    error ZeroPayoutRef();
    error PayoutRefUsed(bytes32 payoutRef);
    error InvalidRefundReason(uint8 reason);
    error InvalidRecipient(address recipient);
    error InsufficientPoolBalance(uint256 available, uint256 required);
    error OnrampAlreadyPaid(bytes32 orderId);
    error DailyEmitCapExceeded(uint256 remaining, uint256 requested);
    error InvalidCompleteDelay(uint256 value);
    error InvalidDisputeWindow(uint256 value);
    error InvalidReclaimDelay(uint256 value);
    error InvalidDelayGap(uint256 completeDelay, uint256 reclaimDelay);

    event OfframpLocked(
        bytes32 indexed id, address indexed user, uint256 amount,
        uint64 inrPaise, uint64 rateE6, bytes32 qrCommitment
    );
    event OfframpCompleted(bytes32 indexed id, uint64 inrPaise, bytes32 payoutRef);
    event OfframpRefunded(bytes32 indexed id, uint8 reason);
    event OfframpReversed(bytes32 indexed id, address indexed user, uint256 amount);
    event OfframpDisputed(bytes32 indexed id, address indexed user, uint256 amount);
    event OfframpReleased(bytes32 indexed id, uint256 amount);
    event DisputeResolved(bytes32 indexed id, bool refundedUser, uint256 amount);
    event DisputeTimedOut(bytes32 indexed id, address indexed user, uint256 amount);
    event DisputeTimeoutUpdated(uint256 oldValue, uint256 newValue);
    event OnrampPaid(bytes32 indexed orderId, address indexed wallet, uint256 amount, uint64 inrPaise);
    event PoolFunded(address indexed from, uint256 amount);
    event PoolWithdrawn(address indexed to, uint256 amount);
    event SurplusSwept(address indexed to, uint256 amount);
    event SettlerUpdated(address indexed oldSettler, address indexed newSettler);
    event QuoteSignerUpdated(address indexed oldQuoteSigner, address indexed newQuoteSigner);
    event ArbiterUpdated(address indexed oldArbiter, address indexed newArbiter);
    event GuardianUpdated(address indexed oldGuardian, address indexed newGuardian);
    event MaxOfframpAmountUpdated(uint256 oldValue, uint256 newValue);
    event DailyEmitCapUpdated(uint256 oldValue, uint256 newValue);
    event CompleteDelayUpdated(uint256 oldValue, uint256 newValue);
    event DisputeWindowUpdated(uint256 oldValue, uint256 newValue);
    event ReclaimDelayUpdated(uint256 oldValue, uint256 newValue);

    modifier onlySettler() {
        if (msg.sender != settler) revert UnauthorizedSettler(msg.sender);
        _;
    }

    modifier onlyArbiter() {
        if (msg.sender != arbiter) revert UnauthorizedArbiter(msg.sender);
        _;
    }

    /// @notice Initializes the token, two-step owner, settler, quote signer and arbiter.
    /// @dev All constructor addresses must be nonzero; guardian initially remains disabled.
    /// Decimals above 26 cannot represent the default 100-token limit in uint96.
    constructor(IERC20 token_, address owner_, address settler_, address quoteSigner_, address arbiter_)
        Ownable(_nonzeroOwner(owner_)) EIP712("ScanPayVault", "1")
    {
        if (
            address(token_) == address(0) || settler_ == address(0)
                || quoteSigner_ == address(0) || arbiter_ == address(0)
        ) revert ZeroAddress();
        token = token_;
        uint8 tokenDecimals = IERC20Metadata(address(token_)).decimals();
        if (tokenDecimals > 26) revert UnsupportedDecimals(tokenDecimals);
        decimals = tokenDecimals;
        settler = settler_;
        quoteSigner = quoteSigner_;
        arbiter = arbiter_;
        uint256 unit = 10 ** uint256(tokenDecimals);
        maxOfframpAmount = 100 * unit;
        dailyEmitCap = 1000 * unit;
        emitWindowStart = block.timestamp;
    }

    /// @notice Returns the full EIP-712 digest both user and quote signer must sign.
    function hashIntent(OfframpIntent calldata intent) public view returns (bytes32 digest) {
        return _hashTypedDataV4(keccak256(abi.encode(
            OFFRAMP_INTENT_TYPEHASH, intent.id, intent.user, intent.amount,
            intent.inrPaise, intent.rateE6, intent.qrCommitment, intent.deadline
        )));
    }

    /// @notice Locks a user-authorized, quote-signed amount; anyone may relay both signatures.
    /// @dev Supports ERC-1271 users. A failed/front-run permit is tolerated only if allowance suffices.
    function offramp(
        OfframpIntent calldata intent, bytes calldata userSig, bytes calldata quoteSig, PermitSig calldata permitSig
    ) external whenNotPaused nonReentrant {
        if (intent.user == address(0)) revert ZeroAddress();
        if (block.timestamp > intent.deadline) revert IntentExpired(intent.deadline);
        if (offramps[intent.id].status != Status.NONE) revert OfframpIdUsed(intent.id);
        if (intent.inrPaise == 0 || intent.rateE6 == 0 || intent.qrCommitment == bytes32(0)) {
            revert InvalidQuote();
        }
        if (intent.amount == 0) revert InvalidAmount(intent.amount);
        if (intent.amount > maxOfframpAmount || intent.amount > type(uint96).max) {
            revert OfframpAmountTooLarge(intent.amount);
        }
        bytes32 digest = hashIntent(intent);
        if (!SignatureChecker.isValidSignatureNow(intent.user, digest, userSig)) {
            revert InvalidSignature();
        }
        (address recovered, ECDSA.RecoverError err,) = ECDSA.tryRecover(digest, quoteSig);
        if (err != ECDSA.RecoverError.NoError || recovered != quoteSigner) revert InvalidQuote();

        offramps[intent.id] = Offramp({
            user: intent.user,
            amount: uint96(intent.amount),
            inrPaise: intent.inrPaise,
            lockedAt: _timestamp(),
            completedAt: 0,
            status: Status.LOCKED,
            qrCommitment: intent.qrCommitment
        });
        deadlines[intent.id] = Deadlines({
            completableAt: _toUint64(block.timestamp + completeDelay),
            reclaimableAt: _toUint64(block.timestamp + reclaimDelay),
            disputeWindow: _toUint64(disputeWindow),
            releasableAt: 0
        });
        escrowedTotal += intent.amount;
        // Commit lock/accounting before permit and transfer; a failure unwinds all effects.
        try IERC20Permit(address(token)).permit(
            intent.user, address(this), intent.amount,
            permitSig.deadline, permitSig.v, permitSig.r, permitSig.s
        ) {} catch {}
        uint256 available = token.allowance(intent.user, address(this));
        if (available < intent.amount) revert InsufficientAllowance(available, intent.amount);

        _pullExact(intent.user, intent.amount);
        emit OfframpLocked(intent.id, intent.user, intent.amount, intent.inrPaise, intent.rateE6, intent.qrCommitment);
    }

    /// @notice Records a unique nonzero fiat payout reference and moves locked funds into held funds.
    /// @dev Available while paused, only within the lock's completion interval; transfers no tokens.
    function completeOfframp(bytes32 id, bytes32 payoutRef) external onlySettler nonReentrant {
        Offramp storage item = _withStatus(id, Status.LOCKED);
        Deadlines storage window = deadlines[id];
        if (block.timestamp < window.completableAt) revert TooEarly(window.completableAt);
        if (block.timestamp >= window.reclaimableAt) revert CompletionWindowClosed(window.reclaimableAt);
        if (payoutRef == bytes32(0)) revert ZeroPayoutRef();
        if (payoutRefUsed[payoutRef]) revert PayoutRefUsed(payoutRef);
        payoutRefUsed[payoutRef] = true;
        item.status = Status.COMPLETED;
        item.completedAt = _timestamp();
        window.releasableAt = _toUint64(block.timestamp + window.disputeWindow);
        escrowedTotal -= item.amount;
        heldTotal += item.amount;
        emit OfframpCompleted(id, item.inrPaise, payoutRef);
    }

    /// @notice Refunds locked escrow to its original user for operator reasons 1 through 4.
    /// @dev Available while paused.
    function refundOfframp(bytes32 id, uint8 reason) external onlySettler nonReentrant {
        if (reason < REASON_INVALID_QR || reason > REASON_OPERATOR) revert InvalidRefundReason(reason);
        Offramp storage item = _withStatus(id, Status.LOCKED);
        item.status = Status.REFUNDED;
        escrowedTotal -= item.amount;
        token.safeTransfer(item.user, item.amount);
        emit OfframpRefunded(id, reason);
    }

    /// @notice Reverses a completed offramp to its original user strictly before release opens.
    /// @dev Draws only held funds, not pool liquidity; available while paused and bypasses the onramp cap.
    function reverseOfframp(bytes32 id) external onlySettler nonReentrant {
        Offramp storage item = _withStatus(id, Status.COMPLETED);
        uint256 expiresAt = deadlines[id].releasableAt;
        if (block.timestamp >= expiresAt) revert DisputeWindowExpired(expiresAt);
        item.status = Status.REVERSED;
        heldTotal -= item.amount;
        token.safeTransfer(item.user, item.amount);
        emit OfframpReversed(id, item.user, item.amount);
    }

    /// @notice Lets anyone return still-locked escrow to its original user once reclaim opens.
    /// @dev Available while paused; emits reason REASON_USER_RECLAIM (5).
    function reclaimOfframp(bytes32 id) external nonReentrant {
        Offramp storage item = _withStatus(id, Status.LOCKED);
        uint256 availableAt = deadlines[id].reclaimableAt;
        if (block.timestamp < availableAt) revert TooEarly(availableAt);
        item.status = Status.REFUNDED;
        escrowedTotal -= item.amount;
        token.safeTransfer(item.user, item.amount);
        emit OfframpRefunded(id, REASON_USER_RECLAIM);
    }

    /// @notice Lets the original user freeze completed funds for arbitration before release opens.
    /// @dev Available while paused; snapshots block.timestamp + disputeTimeout as the resolve deadline.
    /// Overwrites releasableAt, which doubles as the resolve deadline once DISPUTED:
    /// deadlines(id).releasableAt for a DISPUTED offramp returns that deadline.
    /// Later timeout changes never affect this dispute; anyone can claim its timeout to the user.
    function disputeOfframp(bytes32 id) external nonReentrant {
        Offramp storage item = _withStatus(id, Status.COMPLETED);
        if (msg.sender != item.user) revert UnauthorizedUser(msg.sender);
        uint256 expiresAt = deadlines[id].releasableAt;
        if (block.timestamp >= expiresAt) revert DisputeWindowExpired(expiresAt);
        item.status = Status.DISPUTED;
        deadlines[id].releasableAt = _toUint64(block.timestamp + disputeTimeout);
        emit OfframpDisputed(id, item.user, item.amount);
    }

    /// @notice Lets anyone release undisputed completed funds into the pool once the window ends.
    /// @dev Available while paused; transfers no tokens and cannot release disputed funds.
    function releaseOfframp(bytes32 id) external nonReentrant {
        Offramp storage item = _withStatus(id, Status.COMPLETED);
        uint256 availableAt = deadlines[id].releasableAt;
        if (block.timestamp < availableAt) revert TooEarly(availableAt);
        item.status = Status.RELEASED;
        heldTotal -= item.amount;
        poolBalance += item.amount;
        emit OfframpReleased(id, item.amount);
    }

    /// @notice Lets the arbiter refund disputed funds to the user or release them into the pool.
    /// @dev Only strictly before the snapshotted resolve deadline; available while paused.
    /// All effects precede any refund transfer.
    function resolveDispute(bytes32 id, bool refundUser) external onlyArbiter nonReentrant {
        Offramp storage item = _withStatus(id, Status.DISPUTED);
        uint256 resolveDeadline = deadlines[id].releasableAt;
        if (block.timestamp >= resolveDeadline) revert DisputeResolutionExpired(resolveDeadline);
        heldTotal -= item.amount;
        if (refundUser) {
            item.status = Status.REVERSED;
            token.safeTransfer(item.user, item.amount);
        } else {
            item.status = Status.RELEASED;
            poolBalance += item.amount;
        }
        emit DisputeResolved(id, refundUser, item.amount);
    }

    /// @notice Lets anyone return disputed held funds to the original user at/after the resolve deadline.
    /// @dev Available while paused; the caller never receives the refund. Effects precede transfer.
    function claimDisputeTimeout(bytes32 id) external nonReentrant {
        Offramp storage item = _withStatus(id, Status.DISPUTED);
        uint256 resolveDeadline = deadlines[id].releasableAt;
        if (block.timestamp < resolveDeadline) revert TooEarly(resolveDeadline);
        item.status = Status.REVERSED;
        heldTotal -= item.amount;
        token.safeTransfer(item.user, item.amount);
        emit DisputeTimedOut(id, item.user, item.amount);
    }

    /// @notice Pays a unique fiat order from free pool funds within the fixed 24-hour onramp cap.
    function onramp(bytes32 orderId, address wallet, uint256 amount, uint64 inrPaise)
        external onlySettler whenNotPaused nonReentrant
    {
        if (onrampDone[orderId]) revert OnrampAlreadyPaid(orderId);
        if (wallet == address(0) || wallet == address(this)) revert InvalidRecipient(wallet);
        if (amount == 0) revert InvalidAmount(amount);
        _checkPool(amount);
        if (block.timestamp >= emitWindowStart + 1 days) {
            emitWindowStart = block.timestamp;
            emittedInWindow = 0;
        }
        uint256 remaining = remainingDailyCap();
        if (amount > remaining) revert DailyEmitCapExceeded(remaining, amount);
        onrampDone[orderId] = true;
        poolBalance -= amount;
        emittedInWindow += amount;
        token.safeTransfer(wallet, amount);
        emit OnrampPaid(orderId, wallet, amount, inrPaise);
    }

    /// @notice Adds exactly amount tokens to the accounted pool; anyone may fund, even while paused.
    function fundPool(uint256 amount) external nonReentrant {
        if (amount == 0) revert InvalidAmount(amount);
        poolBalance += amount;
        _pullExact(msg.sender, amount);
        emit PoolFunded(msg.sender, amount);
    }

    /// @notice Owner withdrawal from free pool funds only, never escrow or held funds; works while paused.
    function withdrawPool(address to, uint256 amount) external onlyOwner nonReentrant {
        if (to == address(0) || to == address(this)) revert InvalidRecipient(to);
        _checkPool(amount);
        poolBalance -= amount;
        token.safeTransfer(to, amount);
        emit PoolWithdrawn(to, amount);
    }

    /// @notice Sends only unaccounted surplus to a valid recipient; available while paused.
    /// @dev Returns without a transfer or event if the token balance does not exceed accounting.
    function sweepSurplus(address to) external onlyOwner nonReentrant {
        if (to == address(0) || to == address(this)) revert InvalidRecipient(to);
        uint256 balance = token.balanceOf(address(this));
        uint256 accounted = escrowedTotal + heldTotal + poolBalance;
        if (balance <= accounted) return;
        uint256 surplus = balance - accounted;
        token.safeTransfer(to, surplus);
        emit SurplusSwept(to, surplus);
    }

    /// @notice Replaces the settler with a nonzero operator address.
    function setSettler(address newSettler) external onlyOwner {
        if (newSettler == address(0)) revert ZeroAddress();
        address oldSettler = settler;
        settler = newSettler;
        emit SettlerUpdated(oldSettler, newSettler);
    }

    /// @notice Replaces the quote signer with a nonzero ECDSA signer address.
    function setQuoteSigner(address newQuoteSigner) external onlyOwner {
        if (newQuoteSigner == address(0)) revert ZeroAddress();
        address oldQuoteSigner = quoteSigner;
        quoteSigner = newQuoteSigner;
        emit QuoteSignerUpdated(oldQuoteSigner, newQuoteSigner);
    }

    /// @notice Replaces the arbiter with a nonzero dispute resolution address.
    function setArbiter(address newArbiter) external onlyOwner {
        if (newArbiter == address(0)) revert ZeroAddress();
        address oldArbiter = arbiter;
        arbiter = newArbiter;
        emit ArbiterUpdated(oldArbiter, newArbiter);
    }

    /// @notice Sets the pause-only guardian; zero disables guardian access.
    function setGuardian(address newGuardian) external onlyOwner {
        address oldGuardian = guardian;
        guardian = newGuardian;
        emit GuardianUpdated(oldGuardian, newGuardian);
    }

    /// @notice Lets only the guardian pause new offramps/onramps, idempotently; never unpauses.
    function guardianPause() external {
        if (msg.sender != guardian) revert UnauthorizedGuardian(msg.sender);
        if (!paused()) _pause();
    }

    /// @notice Lets only the owner idempotently pause/unpause new offramps and onramps.
    function setPaused(bool paused_) external onlyOwner {
        if (paused_ == paused()) return;
        if (paused_) _pause();
        else _unpause();
    }

    /// @notice Updates both amount limits without resetting the current onramp window.
    /// @dev Zero disables the corresponding new activity; existing recoveries remain available.
    function setLimits(uint256 maxOfframpAmount_, uint256 dailyEmitCap_) external onlyOwner {
        _setMaxOfframpAmount(maxOfframpAmount_);
        _setDailyEmitCap(dailyEmitCap_);
    }

    /// @notice Sets the per-offramp limit, which must fit uint96.
    function setMaxOfframpAmount(uint256 value) external onlyOwner {
        _setMaxOfframpAmount(value);
    }

    /// @notice Sets the onramp payout cap without resetting already-consumed capacity.
    function setDailyEmitCap(uint256 value) external onlyOwner {
        _setDailyEmitCap(value);
    }

    /// @notice Sets the completion delay for new locks, at most one day and an hour before reclaim.
    function setCompleteDelay(uint256 value) external onlyOwner {
        if (value > 1 days) revert InvalidCompleteDelay(value);
        if (value + 1 hours > reclaimDelay) revert InvalidDelayGap(value, reclaimDelay);
        uint256 oldValue = completeDelay;
        completeDelay = value;
        emit CompleteDelayUpdated(oldValue, value);
    }

    /// @notice Sets the dispute duration (MIN_DISPUTE_WINDOW to 30 days) for offramps locked from now on.
    /// @dev The floor stops a compromised owner from removing user recourse before executing signed intents.
    function setDisputeWindow(uint256 value) external onlyOwner {
        if (value < MIN_DISPUTE_WINDOW || value > 30 days) revert InvalidDisputeWindow(value);
        uint256 oldValue = disputeWindow;
        disputeWindow = value;
        emit DisputeWindowUpdated(oldValue, value);
    }

    /// @notice Sets the resolution timeout (1 to 30 days) for disputes opened from now on.
    /// @dev Existing disputes keep their snapshotted resolve deadlines.
    function setDisputeTimeout(uint256 value) external onlyOwner {
        if (value < MIN_DISPUTE_TIMEOUT || value > MAX_DISPUTE_TIMEOUT) revert InvalidDisputeTimeout(value);
        uint256 oldValue = disputeTimeout;
        disputeTimeout = value;
        emit DisputeTimeoutUpdated(oldValue, value);
    }

    /// @notice Sets reclaim delay for new locks (1 hour to 30 days), at least an hour after completion opens.
    function setReclaimDelay(uint256 value) external onlyOwner {
        if (value < 1 hours || value > 30 days) revert InvalidReclaimDelay(value);
        if (completeDelay + 1 hours > value) revert InvalidDelayGap(completeDelay, value);
        uint256 oldValue = reclaimDelay;
        reclaimDelay = value;
        emit ReclaimDelayUpdated(oldValue, value);
    }

    /// @notice Returns unused capacity for the current fixed 24-hour onramp window.
    /// @dev Not a rolling window or UTC day. At/after start + 1 day, capacity is the full
    /// cap; the next successful onramp starts a fresh window at that block's timestamp.
    /// Lowering the cap below usage returns zero, not an arithmetic underflow.
    function remainingDailyCap() public view returns (uint256) {
        if (block.timestamp >= emitWindowStart + 1 days) return dailyEmitCap;
        return emittedInWindow >= dailyEmitCap ? 0 : dailyEmitCap - emittedInWindow;
    }

    /// @notice Ownership cannot be renounced; use the inherited two-step transfer instead.
    function renounceOwnership() public override onlyOwner {
        revert OwnershipRenunciationDisabled();
    }

    function _pullExact(address from, uint256 amount) internal {
        uint256 beforeBalance = token.balanceOf(address(this));
        token.safeTransferFrom(from, address(this), amount);
        uint256 afterBalance = token.balanceOf(address(this));
        if (afterBalance < beforeBalance || afterBalance - beforeBalance != amount) {
            revert ReceivedAmountMismatch(amount, beforeBalance, afterBalance);
        }
    }

    function _withStatus(bytes32 id, Status expected) internal view returns (Offramp storage item) {
        item = offramps[id];
        if (item.status != expected) revert InvalidStatus(id, item.status, expected);
    }

    function _checkPool(uint256 amount) internal view {
        if (amount > poolBalance) revert InsufficientPoolBalance(poolBalance, amount);
    }

    function _timestamp() internal view returns (uint64) {
        return _toUint64(block.timestamp);
    }

    function _toUint64(uint256 value) internal pure returns (uint64) {
        if (value > type(uint64).max) revert TimestampOutOfRange();
        return uint64(value);
    }

    /// @dev Validate before Ownable's constructor so a zero owner also uses ZeroAddress.
    function _nonzeroOwner(address owner_) private pure returns (address) {
        if (owner_ == address(0)) revert ZeroAddress();
        return owner_;
    }

    function _setMaxOfframpAmount(uint256 value) internal {
        if (value > type(uint96).max) revert OfframpAmountTooLarge(value);
        uint256 oldValue = maxOfframpAmount;
        maxOfframpAmount = value;
        emit MaxOfframpAmountUpdated(oldValue, value);
    }

    function _setDailyEmitCap(uint256 value) internal {
        uint256 oldValue = dailyEmitCap;
        dailyEmitCap = value;
        emit DailyEmitCapUpdated(oldValue, value);
    }
}
