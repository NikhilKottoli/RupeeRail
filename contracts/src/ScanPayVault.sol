// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Ownable2Step} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {IERC20Permit} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Permit.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {SignatureChecker} from "@openzeppelin/contracts/utils/cryptography/SignatureChecker.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";

/**
 * @title ScanPayVault
 * @notice Single-token escrow and payout pool; deploy one vault per chain.
 * @dev Uses only ERC-20 calls, including on Arc where the token is also the gas coin.
 * At successful transaction boundaries, for a non-rebasing, exact-transfer token:
 * token.balanceOf(address(this)) >= escrowedTotal + poolBalance.
 * Status transitions are NONE -> LOCKED -> COMPLETED -> REVERSED or LOCKED -> REFUNDED.
 * The settler can only move escrow into the pool or refund it to the original user;
 * reversals also pay only the original user and require available pool liquidity.
 * The trusted settler can drain the pool up to the shared daily payout cap; the
 * owner can withdraw the pool and change configuration. emitPayment has no ID:
 * retrying pays twice. Use onramp for real orders. The signed rate is recorded in
 * OfframpLocked, not enforced. Offramp ids are first-come: ids are globally unique and not
 * bound to a user, so an observer of a pending relayed transaction can front-run it with their
 * own intent (and their own tokens) under the same id. The victim's transaction then reverts
 * with OfframpIdUsed and the victim must re-quote with a fresh id; the squatter's escrow is
 * refundable but the id stays burned. Mitigations belong to the relayer: submit through a
 * private/ordered channel and refund any OfframpLocked whose user or amount is unexpected
 * (phase 2: a backend quote signature verified by the vault removes this).
 * Direct token transfers are unaccounted surplus,
 * not pool funding. Fee-on-transfer deposits are rejected; rebasing or otherwise
 * externally debitable tokens are unsupported. Token blocklists may prevent refunds.
 * reclaimDelay and disputeWindow are both snapshotted when an offramp is locked,
 * so later owner changes never alter the recovery terms of funds already committed;
 * completeDelay is read live (it can only delay the settler, and users can always reclaim).
 */
contract ScanPayVault is Ownable2Step, EIP712, ReentrancyGuard, Pausable {
    using SafeERC20 for IERC20;

    struct OfframpIntent {
        bytes32 id;
        address user;
        uint256 amount;
        uint64 inrPaise;
        uint64 rateE6;
        string qrText;
        uint64 deadline;
    }

    struct PermitSig {
        uint256 deadline;
        uint8 v;
        bytes32 r;
        bytes32 s;
    }

    enum Status { NONE, LOCKED, COMPLETED, REFUNDED, REVERSED }

    /// @dev Per-offramp deadlines snapshotted from the owner-set delays at the time they begin.
    struct Deadlines {
        uint64 reclaimableAt;   // lockedAt + reclaimDelay at lock time
        uint64 reversibleUntil; // completedAt + snapshotted dispute window, set at completion
        uint64 disputeWindow;   // disputeWindow at lock time
    }

    struct Offramp {
        address user;
        uint96 amount;
        uint64 inrPaise;
        uint64 lockedAt;
        uint64 completedAt;
        Status status;
        bytes32 qrHash;
    }

    bytes32 public constant OFFRAMP_INTENT_TYPEHASH = keccak256(
        "OfframpIntent(bytes32 id,address user,uint256 amount,uint64 inrPaise,uint64 rateE6,string qrText,uint64 deadline)"
    );
    uint8 public constant REASON_INVALID_QR = 1;
    uint8 public constant REASON_AMOUNT_MISMATCH = 2;
    uint8 public constant REASON_DISPATCH_FAILED = 3;
    uint8 public constant REASON_OPERATOR = 4;
    uint8 public constant REASON_USER_RECLAIM = 5;

    IERC20 public immutable token;
    uint8 public immutable decimals;
    address public settler;
    mapping(bytes32 => Offramp) public offramps;
    mapping(bytes32 => Deadlines) public deadlines;
    mapping(bytes32 => bool) public onrampDone;
    uint256 public escrowedTotal;
    uint256 public poolBalance;

    uint256 public completeDelay = 15 seconds;
    uint256 public disputeWindow = 24 hours;
    uint256 public reclaimDelay = 24 hours;
    uint256 public maxOfframpAmount;
    uint256 public dailyEmitCap;
    /// @notice Start of the current fixed 24-hour payout window, initially deployment time.
    uint256 public emitWindowStart;
    uint256 public emittedInWindow;

    error ZeroAddress();
    error UnauthorizedSettler(address caller);
    error UnauthorizedUser(address caller);
    error OwnershipRenunciationDisabled();
    error UnsupportedDecimals(uint8 tokenDecimals);
    error TimestampOutOfRange();
    error IntentExpired(uint64 deadline);
    error OfframpIdUsed(bytes32 id);
    error InvalidQrLength(uint256 length);
    error InvalidAmount(uint256 amount);
    error OfframpAmountTooLarge(uint256 amount);
    error InvalidSignature();
    error InsufficientAllowance(uint256 available, uint256 required);
    error ReceivedAmountMismatch(uint256 expected, uint256 balanceBefore, uint256 balanceAfter);
    error InvalidStatus(bytes32 id, Status actual, Status expected);
    error TooEarly(uint256 availableAt);
    error DisputeWindowExpired(uint256 expiredAt);
    error InvalidRefundReason(uint8 reason);
    error InsufficientPoolBalance(uint256 available, uint256 required);
    error OnrampAlreadyPaid(bytes32 orderId);
    error DailyEmitCapExceeded(uint256 remaining, uint256 requested);
    error InvalidCompleteDelay(uint256 value);
    error InvalidDisputeWindow(uint256 value);
    error InvalidReclaimDelay(uint256 value);

    event OfframpLocked(
        bytes32 indexed id, address indexed user, uint256 amount,
        uint64 inrPaise, uint64 rateE6, string qrText
    );
    event OfframpCompleted(bytes32 indexed id, uint64 inrPaise, bytes32 payoutRef);
    event OfframpRefunded(bytes32 indexed id, uint8 reason);
    event OfframpReversed(bytes32 indexed id, address indexed user, uint256 amount);
    event OnrampPaid(bytes32 indexed orderId, address indexed wallet, uint256 amount, uint64 inrPaise);
    event PaymentEmitted(address indexed wallet, uint256 amount);
    event PoolFunded(address indexed from, uint256 amount);
    event PoolWithdrawn(address indexed to, uint256 amount);
    event SettlerUpdated(address indexed oldSettler, address indexed newSettler);
    event MaxOfframpAmountUpdated(uint256 oldValue, uint256 newValue);
    event DailyEmitCapUpdated(uint256 oldValue, uint256 newValue);
    event CompleteDelayUpdated(uint256 oldValue, uint256 newValue);
    event DisputeWindowUpdated(uint256 oldValue, uint256 newValue);
    event ReclaimDelayUpdated(uint256 oldValue, uint256 newValue);

    modifier onlySettler() {
        if (msg.sender != settler) revert UnauthorizedSettler(msg.sender);
        _;
    }

    /// @notice Initializes the token, two-step owner and settlement operator.
    /// @dev Decimals above 26 cannot represent the default 100-token limit in uint96.
    constructor(IERC20 token_, address owner_, address settler_)
        Ownable(owner_) EIP712("ScanPayVault", "1")
    {
        if (address(token_) == address(0) || owner_ == address(0) || settler_ == address(0)) {
            revert ZeroAddress();
        }
        token = token_;
        uint8 tokenDecimals = IERC20Metadata(address(token_)).decimals();
        if (tokenDecimals > 26) revert UnsupportedDecimals(tokenDecimals);
        decimals = tokenDecimals;
        settler = settler_;
        uint256 unit = 10 ** uint256(tokenDecimals);
        maxOfframpAmount = 100 * unit;
        dailyEmitCap = 1000 * unit;
        emitWindowStart = block.timestamp;
    }

    /// @notice Returns the full EIP-712 digest to sign, bound to this vault and chain.
    function hashIntent(OfframpIntent calldata intent) public view returns (bytes32 digest) {
        return _hashTypedDataV4(keccak256(abi.encode(
            OFFRAMP_INTENT_TYPEHASH, intent.id, intent.user, intent.amount,
            intent.inrPaise, intent.rateE6, keccak256(bytes(intent.qrText)), intent.deadline
        )));
    }

    /// @notice Locks the user's signed amount; anyone may relay the intent and permit.
    /// @dev A failed/front-run permit is tolerated only if sufficient allowance exists.
    function offramp(OfframpIntent calldata intent, bytes calldata userSig, PermitSig calldata permitSig)
        external whenNotPaused nonReentrant
    {
        if (intent.user == address(0)) revert ZeroAddress();
        if (block.timestamp > intent.deadline) revert IntentExpired(intent.deadline);
        if (offramps[intent.id].status != Status.NONE) revert OfframpIdUsed(intent.id);
        uint256 qrLength = bytes(intent.qrText).length;
        if (qrLength == 0 || qrLength > 512) revert InvalidQrLength(qrLength);
        if (intent.amount == 0) revert InvalidAmount(intent.amount);
        if (intent.amount > maxOfframpAmount || intent.amount > type(uint96).max) {
            revert OfframpAmountTooLarge(intent.amount);
        }
        if (!SignatureChecker.isValidSignatureNow(intent.user, hashIntent(intent), userSig)) {
            revert InvalidSignature();
        }

        offramps[intent.id] = Offramp({
            user: intent.user,
            amount: uint96(intent.amount),
            inrPaise: intent.inrPaise,
            lockedAt: _timestamp(),
            completedAt: 0,
            status: Status.LOCKED,
            qrHash: keccak256(bytes(intent.qrText))
        });
        deadlines[intent.id] = Deadlines({
            reclaimableAt: _toUint64(block.timestamp + reclaimDelay),
            reversibleUntil: 0,
            disputeWindow: _toUint64(disputeWindow)
        });
        escrowedTotal += intent.amount;
        // Effects are written first (checks-effects-interactions); every revert below unwinds
        // them. The permit may be front-run, so only the resulting allowance matters.
        try IERC20Permit(address(token)).permit(
            intent.user, address(this), intent.amount,
            permitSig.deadline, permitSig.v, permitSig.r, permitSig.s
        ) {} catch {}
        uint256 available = token.allowance(intent.user, address(this));
        if (available < intent.amount) revert InsufficientAllowance(available, intent.amount);

        _pullExact(intent.user, intent.amount);
        emit OfframpLocked(intent.id, intent.user, intent.amount, intent.inrPaise, intent.rateE6, intent.qrText);
    }

    /// @notice Records a fiat payout after the completion delay and moves escrow to the pool.
    /// @dev Available while paused. Does not transfer tokens or verify the fiat reference.
    function completeOfframp(bytes32 id, bytes32 payoutRef) external onlySettler nonReentrant {
        Offramp storage item = _withStatus(id, Status.LOCKED);
        uint256 availableAt = uint256(item.lockedAt) + completeDelay;
        if (block.timestamp < availableAt) revert TooEarly(availableAt);
        item.status = Status.COMPLETED;
        item.completedAt = _timestamp();
        Deadlines storage window = deadlines[id];
        window.reversibleUntil = _toUint64(block.timestamp + window.disputeWindow);
        escrowedTotal -= item.amount;
        poolBalance += item.amount;
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

    /// @notice Reverses a completed offramp to its original user within the dispute window.
    /// @dev Requires pool liquidity and is available while paused; bypasses the payout cap.
    function reverseOfframp(bytes32 id) external onlySettler nonReentrant {
        Offramp storage item = _withStatus(id, Status.COMPLETED);
        uint256 expiresAt = deadlines[id].reversibleUntil;
        if (block.timestamp > expiresAt) revert DisputeWindowExpired(expiresAt);
        _checkPool(item.amount);
        item.status = Status.REVERSED;
        poolBalance -= item.amount;
        token.safeTransfer(item.user, item.amount);
        emit OfframpReversed(id, item.user, item.amount);
    }

    /// @notice Lets the original user recover still-locked escrow after the reclaim delay.
    /// @dev Available while paused; emits reason REASON_USER_RECLAIM (5).
    function reclaimOfframp(bytes32 id) external nonReentrant {
        Offramp storage item = _withStatus(id, Status.LOCKED);
        if (msg.sender != item.user) revert UnauthorizedUser(msg.sender);
        uint256 availableAt = deadlines[id].reclaimableAt;
        if (block.timestamp < availableAt) revert TooEarly(availableAt);
        item.status = Status.REFUNDED;
        escrowedTotal -= item.amount;
        token.safeTransfer(item.user, item.amount);
        emit OfframpRefunded(id, REASON_USER_RECLAIM);
    }

    /// @notice Pays a unique fiat order from the pool, sharing the fixed-window daily cap.
    function onramp(bytes32 orderId, address wallet, uint256 amount, uint64 inrPaise)
        external onlySettler nonReentrant
    {
        if (onrampDone[orderId]) revert OnrampAlreadyPaid(orderId);
        onrampDone[orderId] = true;
        _emitPayment(wallet, amount);
        emit OnrampPaid(orderId, wallet, amount, inrPaise);
    }

    /// @notice Pays from the pool without an order ID. Retrying this call pays twice.
    function emitPayment(address wallet, uint256 amount) external onlySettler nonReentrant {
        _emitPayment(wallet, amount);
        emit PaymentEmitted(wallet, amount);
    }

    /// @notice Adds exactly amount tokens to the accounted pool; anyone may fund, even while paused.
    function fundPool(uint256 amount) external nonReentrant {
        if (amount == 0) revert InvalidAmount(amount);
        poolBalance += amount;
        _pullExact(msg.sender, amount);
        emit PoolFunded(msg.sender, amount);
    }

    /// @notice Owner withdrawal from the pool only, never escrow; available while paused.
    function withdrawPool(address to, uint256 amount) external onlyOwner nonReentrant {
        if (to == address(0)) revert ZeroAddress();
        _checkPool(amount);
        poolBalance -= amount;
        token.safeTransfer(to, amount);
        emit PoolWithdrawn(to, amount);
    }

    /// @notice Replaces the settler with a nonzero operator address.
    function setSettler(address newSettler) external onlyOwner {
        if (newSettler == address(0)) revert ZeroAddress();
        address oldSettler = settler;
        settler = newSettler;
        emit SettlerUpdated(oldSettler, newSettler);
    }

    /// @notice Idempotently pauses/unpauses new offramps and pool payouts, not recovery paths.
    function setPaused(bool paused_) external onlyOwner {
        if (paused_ == paused()) return;
        if (paused_) _pause();
        else _unpause();
    }

    /// @notice Updates both amount limits without resetting the current emission window.
    /// @dev Zero disables the corresponding new activity; existing recoveries remain available.
    function setLimits(uint256 maxOfframpAmount_, uint256 dailyEmitCap_) external onlyOwner {
        _setMaxOfframpAmount(maxOfframpAmount_);
        _setDailyEmitCap(dailyEmitCap_);
    }

    /// @notice Sets the per-offramp limit, which must fit uint96.
    function setMaxOfframpAmount(uint256 value) external onlyOwner {
        _setMaxOfframpAmount(value);
    }

    /// @notice Sets the shared payout cap without resetting already-consumed capacity.
    function setDailyEmitCap(uint256 value) external onlyOwner {
        _setDailyEmitCap(value);
    }

    /// @notice Sets the completion delay, at most one day; also applies to existing locks.
    function setCompleteDelay(uint256 value) external onlyOwner {
        if (value > 1 days) revert InvalidCompleteDelay(value);
        uint256 oldValue = completeDelay;
        completeDelay = value;
        emit CompleteDelayUpdated(oldValue, value);
    }

    /// @notice Sets the reversal window (at most 30 days) for offramps locked from now on.
    function setDisputeWindow(uint256 value) external onlyOwner {
        if (value > 30 days) revert InvalidDisputeWindow(value);
        uint256 oldValue = disputeWindow;
        disputeWindow = value;
        emit DisputeWindowUpdated(oldValue, value);
    }

    /// @notice Sets the user reclaim delay (1 hour to 30 days) for offramps locked from now on.
    function setReclaimDelay(uint256 value) external onlyOwner {
        if (value < 1 hours || value > 30 days) revert InvalidReclaimDelay(value);
        uint256 oldValue = reclaimDelay;
        reclaimDelay = value;
        emit ReclaimDelayUpdated(oldValue, value);
    }

    /// @notice Returns unused capacity for the current fixed 24-hour payout window.
    /// @dev Not a rolling window or UTC day. At/after start + 1 day, capacity is the full
    /// cap; the next successful payout starts a fresh window at that block's timestamp.
    /// Lowering the cap below usage returns zero, not an arithmetic underflow.
    function remainingDailyCap() public view returns (uint256) {
        if (block.timestamp >= emitWindowStart + 1 days) return dailyEmitCap;
        return emittedInWindow >= dailyEmitCap ? 0 : dailyEmitCap - emittedInWindow;
    }

    /// @notice Ownership cannot be renounced; use the inherited two-step transfer instead.
    function renounceOwnership() public override onlyOwner {
        revert OwnershipRenunciationDisabled();
    }

    /// @dev Shared fixed 24-hour window for onramp/emitPayment; never a rolling window.
    /// The first successful payout after expiry starts a new window at block.timestamp.
    function _emitPayment(address wallet, uint256 amount) internal whenNotPaused {
        if (wallet == address(0)) revert ZeroAddress();
        if (amount == 0) revert InvalidAmount(amount);
        _checkPool(amount);
        if (block.timestamp >= emitWindowStart + 1 days) {
            emitWindowStart = block.timestamp;
            emittedInWindow = 0;
        }
        uint256 remaining = remainingDailyCap();
        if (amount > remaining) revert DailyEmitCapExceeded(remaining, amount);
        poolBalance -= amount;
        emittedInWindow += amount;
        token.safeTransfer(wallet, amount);
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
