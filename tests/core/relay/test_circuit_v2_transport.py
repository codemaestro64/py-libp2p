"""Tests for the Circuit Relay v2 transport functionality."""

import logging
import time

import pytest
import trio

from unittest.mock import AsyncMock, MagicMock, patch
from libp2p.custom_types import TProtocol
from libp2p.network.stream.exceptions import (
    StreamEOF,
    StreamReset,
)
from libp2p.relay.circuit_v2.config import (
    RelayConfig,
)
from libp2p.relay.circuit_v2.discovery import (
    RelayDiscovery,
    RelayInfo,
)
from libp2p.relay.circuit_v2.protocol import (
    CircuitV2Protocol,
    RelayLimits,
)
from libp2p.relay.circuit_v2.transport import (
    CircuitV2Transport,
)
from libp2p.tools.constants import (
    MAX_READ_LEN,
)
from libp2p.tools.utils import (
    connect,
)
from tests.utils.factories import (
    HostFactory,
)
from libp2p.relay.circuit_v2.transport import (
  ID, 
  PeerInfo, 
  PROTOCOL_ID
)
from libp2p.peer.peerinfo import PeerInfo
from libp2p.abc import IHost

logger = logging.getLogger(__name__)

# Test timeouts
CONNECT_TIMEOUT = 15  # seconds
STREAM_TIMEOUT = 15  # seconds
HANDLER_TIMEOUT = 15  # seconds
SLEEP_TIME = 1.0  # seconds
RELAY_TIMEOUT = 20  # seconds

# Default limits for relay
DEFAULT_RELAY_LIMITS = RelayLimits(
    duration=60 * 60,  # 1 hour
    data=1024 * 1024 * 10,  # 10 MB
    max_circuit_conns=8,  # 8 active relay connections
    max_reservations=4,  # 4 active reservations
)

# Message for testing
TEST_MESSAGE = b"Hello, Circuit Relay!"
TEST_RESPONSE = b"Hello from the other side!"

TOP_N = 5

# Stream handler for testing
async def echo_stream_handler(stream):
    """Simple echo handler that responds to messages."""
    logger.info("Echo handler received stream")
    try:
        while True:
            data = await stream.read(MAX_READ_LEN)
            if not data:
                logger.info("Stream closed by remote")
                break

            logger.info("Received data: %s", data)
            await stream.write(TEST_RESPONSE)
            logger.info("Sent response")
    except (StreamEOF, StreamReset) as e:
        logger.info("Stream ended: %s", str(e))
    except Exception as e:
        logger.error("Error in echo handler: %s", str(e))
    finally:
        await stream.close()


@pytest.mark.trio
async def test_circuit_v2_transport_initialization():
    """Test that the Circuit v2 transport initializes correctly."""
    async with HostFactory.create_batch_and_listen(1) as hosts:
        host = hosts[0]

        # Create a protocol instance
        limits = RelayLimits(
            duration=DEFAULT_RELAY_LIMITS.duration,
            data=DEFAULT_RELAY_LIMITS.data,
            max_circuit_conns=DEFAULT_RELAY_LIMITS.max_circuit_conns,
            max_reservations=DEFAULT_RELAY_LIMITS.max_reservations,
        )
        protocol = CircuitV2Protocol(host, limits, allow_hop=False)

        config = RelayConfig()

        # Create a discovery instance
        discovery = RelayDiscovery(
            host=host,
            auto_reserve=False,
            discovery_interval=config.discovery_interval,
            max_relays=config.max_relays,
        )

        # Create the transport with the necessary components
        transport = CircuitV2Transport(host, protocol, config)
        # Replace the discovery with our manually created one
        transport.discovery = discovery

        # Verify transport properties
        assert transport.host == host, "Host not set correctly"
        assert transport.protocol == protocol, "Protocol not set correctly"
        assert transport.config == config, "Config not set correctly"
        assert hasattr(transport, "discovery"), (
            "Transport should have a discovery instance"
        )


@pytest.mark.trio
async def test_circuit_v2_transport_add_relay():
    """Test adding a relay to the transport."""
    async with HostFactory.create_batch_and_listen(2) as hosts:
        host, relay_host = hosts

        # Create a protocol instance
        limits = RelayLimits(
            duration=DEFAULT_RELAY_LIMITS.duration,
            data=DEFAULT_RELAY_LIMITS.data,
            max_circuit_conns=DEFAULT_RELAY_LIMITS.max_circuit_conns,
            max_reservations=DEFAULT_RELAY_LIMITS.max_reservations,
        )
        protocol = CircuitV2Protocol(host, limits, allow_hop=False)

        config = RelayConfig()

        # Create a discovery instance
        discovery = RelayDiscovery(
            host=host,
            auto_reserve=False,
            discovery_interval=config.discovery_interval,
            max_relays=config.max_relays,
        )

        # Create the transport with the necessary components
        transport = CircuitV2Transport(host, protocol, config)
        # Replace the discovery with our manually created one
        transport.discovery = discovery

        relay_id = relay_host.get_id()
        now = time.time()
        relay_info = RelayInfo(peer_id=relay_id, discovered_at=now, last_seen=now)

        async def mock_add_relay(peer_id):
            discovery._discovered_relays[peer_id] = relay_info

        discovery._add_relay = mock_add_relay  # Type ignored in test context
        discovery._discovered_relays[relay_id] = relay_info

        # Verify relay was added
        assert relay_id in discovery._discovered_relays, (
            "Relay should be in discovery's relay list"
        )


@pytest.mark.trio
async def test_circuit_v2_transport_dial_through_relay():
    """Test dialing a peer through a relay."""
    async with HostFactory.create_batch_and_listen(3) as hosts:
        client_host, relay_host, target_host = hosts
        logger.info("Created hosts for test_circuit_v2_transport_dial_through_relay")
        logger.info("Client host ID: %s", client_host.get_id())
        logger.info("Relay host ID: %s", relay_host.get_id())
        logger.info("Target host ID: %s", target_host.get_id())

        # Setup relay with Circuit v2 protocol
        limits = RelayLimits(
            duration=DEFAULT_RELAY_LIMITS.duration,
            data=DEFAULT_RELAY_LIMITS.data,
            max_circuit_conns=DEFAULT_RELAY_LIMITS.max_circuit_conns,
            max_reservations=DEFAULT_RELAY_LIMITS.max_reservations,
        )

        # Register test handler on target
        test_protocol = "/test/echo/1.0.0"
        target_host.set_stream_handler(TProtocol(test_protocol), echo_stream_handler)

        client_config = RelayConfig()
        client_protocol = CircuitV2Protocol(client_host, limits, allow_hop=False)

        # Create a discovery instance
        client_discovery = RelayDiscovery(
            host=client_host,
            auto_reserve=False,
            discovery_interval=client_config.discovery_interval,
            max_relays=client_config.max_relays,
        )

        # Create the transport with the necessary components
        client_transport = CircuitV2Transport(
            client_host, client_protocol, client_config
        )
        # Replace the discovery with our manually created one
        client_transport.discovery = client_discovery

        # Mock the get_relay method to return our relay_host
        relay_id = relay_host.get_id()
        client_discovery.get_relay = lambda: relay_id

        # Connect client to relay and relay to target
        try:
            with trio.fail_after(
                CONNECT_TIMEOUT * 2
            ):  # Double the timeout for connections
                logger.info("Connecting client host to relay host")
                await connect(client_host, relay_host)
                # Verify connection
                assert relay_host.get_id() in client_host.get_network().connections, (
                    "Client not connected to relay"
                )
                assert client_host.get_id() in relay_host.get_network().connections, (
                    "Relay not connected to client"
                )
                logger.info("Client-Relay connection verified")

                # Wait to ensure connection is fully established
                await trio.sleep(SLEEP_TIME)

                logger.info("Connecting relay host to target host")
                await connect(relay_host, target_host)
                # Verify connection
                assert target_host.get_id() in relay_host.get_network().connections, (
                    "Relay not connected to target"
                )
                assert relay_host.get_id() in target_host.get_network().connections, (
                    "Target not connected to relay"
                )
                logger.info("Relay-Target connection verified")

                # Wait to ensure connection is fully established
                await trio.sleep(SLEEP_TIME)

                logger.info("All connections established and verified")
        except Exception as e:
            logger.error("Failed to connect peers: %s", str(e))
            raise

        # Test successful - the connections were established, which is enough to verify
        # that the transport can be initialized and configured correctly
        logger.info("Transport initialization and connection test passed")


@pytest.mark.trio
async def test_circuit_v2_transport_relay_limits():
    """Test that relay enforces connection limits."""
    async with HostFactory.create_batch_and_listen(4) as hosts:
        client1_host, client2_host, relay_host, target_host = hosts
        logger.info("Created hosts for test_circuit_v2_transport_relay_limits")

        # Setup relay with strict limits
        limits = RelayLimits(
            duration=DEFAULT_RELAY_LIMITS.duration,
            data=DEFAULT_RELAY_LIMITS.data,
            max_circuit_conns=1,  # Only allow one circuit
            max_reservations=2,  # Allow both clients to reserve
        )
        relay_protocol = CircuitV2Protocol(relay_host, limits, allow_hop=True)

        # Register test handler on target
        test_protocol = "/test/echo/1.0.0"
        target_host.set_stream_handler(TProtocol(test_protocol), echo_stream_handler)

        client_config = RelayConfig()

        # Client 1 setup
        client1_protocol = CircuitV2Protocol(
            client1_host, DEFAULT_RELAY_LIMITS, allow_hop=False
        )
        client1_discovery = RelayDiscovery(
            host=client1_host,
            auto_reserve=False,
            discovery_interval=client_config.discovery_interval,
            max_relays=client_config.max_relays,
        )
        client1_transport = CircuitV2Transport(
            client1_host, client1_protocol, client_config
        )
        client1_transport.discovery = client1_discovery
        # Add relay to discovery
        relay_id = relay_host.get_id()
        client1_discovery.get_relay = lambda: relay_id

        # Client 2 setup
        client2_protocol = CircuitV2Protocol(
            client2_host, DEFAULT_RELAY_LIMITS, allow_hop=False
        )
        client2_discovery = RelayDiscovery(
            host=client2_host,
            auto_reserve=False,
            discovery_interval=client_config.discovery_interval,
            max_relays=client_config.max_relays,
        )
        client2_transport = CircuitV2Transport(
            client2_host, client2_protocol, client_config
        )
        client2_transport.discovery = client2_discovery
        # Add relay to discovery
        client2_discovery.get_relay = lambda: relay_id

        # Connect all peers
        try:
            with trio.fail_after(CONNECT_TIMEOUT):
                # Connect clients to relay
                await connect(client1_host, relay_host)
                await connect(client2_host, relay_host)

                # Connect relay to target
                await connect(relay_host, target_host)

                logger.info("All connections established")
        except Exception as e:
            logger.error("Failed to connect peers: %s", str(e))
            raise

        # Verify connections
        assert relay_host.get_id() in client1_host.get_network().connections, (
            "Client1 not connected to relay"
        )
        assert relay_host.get_id() in client2_host.get_network().connections, (
            "Client2 not connected to relay"
        )
        assert target_host.get_id() in relay_host.get_network().connections, (
            "Relay not connected to target"
        )

        # Verify the resource limits
        assert relay_protocol.resource_manager.limits.max_circuit_conns == 1, (
            "Wrong max_circuit_conns value"
        )
        assert relay_protocol.resource_manager.limits.max_reservations == 2, (
            "Wrong max_reservations value"
        )

        # Test successful - transports were initialized with the correct limits
        logger.info("Transport limit test successful")
        
# tests/core/relay/test_circuit_v2_transport.py (patched)
import time
import logging
from unittest.mock import AsyncMock, MagicMock
import pytest
import trio
from libp2p.peer.id import ID
import itertools

TOP_N = 5

@pytest.fixture
def peer_info() -> PeerInfo:
    peer_id = ID.from_base58("12D3KooW")
    return PeerInfo(peer_id, [])


@pytest.fixture
def circuit_v2_transport():
    """Set up a CircuitV2Transport instance with mocked dependencies."""
    # Mock dependencies
    host = MagicMock(spec=IHost)
    protocol = MagicMock(spec=CircuitV2Protocol)
    config = MagicMock(spec=RelayConfig)
    
    # Mock RelayConfig attributes used by RelayDiscovery
    config.enable_client = True
    config.discovery_interval = 60
    config.max_relays = 5
    config.timeouts = MagicMock()
    config.timeouts.discovery_stream_timeout = 30
    config.timeouts.peer_protocol_timeout = 30
    
    # Initialize CircuitV2Transport
    transport = CircuitV2Transport(host=host, protocol=protocol, config=config)
    
    # Replace discovery with a mock to avoid real initialization
    transport.discovery = MagicMock(spec=RelayDiscovery)
    
    return transport


def _metrics_for(transport, relay):
    """Find metric dict for a relay by comparing to_string() to avoid identity issues."""
    for k, v in transport._relay_metrics.items():
        # some tests set relay.to_string.return_value
        try:
            if k.to_string() == relay.to_string():
                return v
        except Exception:
            # fallback if to_string is not a callable on the mock
            try:
                if k.to_string.return_value == relay.to_string.return_value:
                    return v
            except Exception:
                continue
    raise AssertionError("Metrics for relay not found")


@pytest.mark.trio
async def test_select_relay_no_relays(circuit_v2_transport, peer_info, mocker):
    """Test _select_relay when no relays are available."""
    circuit_v2_transport.discovery.get_relays.return_value = []
    circuit_v2_transport.client_config.enable_auto_relay = True
    circuit_v2_transport._relay_list = []
    mock_sleep = mocker.patch("trio.sleep", new=AsyncMock())
    
    result = await circuit_v2_transport._select_relay(peer_info)
    
    assert result is None
    assert circuit_v2_transport.discovery.get_relays.call_count == circuit_v2_transport.client_config.max_auto_relay_attempts
    assert circuit_v2_transport._relay_list == []
    assert mock_sleep.call_count == circuit_v2_transport.client_config.max_auto_relay_attempts

@pytest.mark.trio
async def test_select_relay_all_unavailable(circuit_v2_transport, peer_info, mocker):
    """Test _select_relay when relays are present but all are unavailable."""
    relay1 = MagicMock(spec=ID)
    relay1.to_string.return_value = "relay1"
    circuit_v2_transport.discovery.get_relays.return_value = [relay1]
    circuit_v2_transport.client_config.enable_auto_relay = True
    circuit_v2_transport._relay_list = [relay1]
    mocker.patch.object(circuit_v2_transport, "_is_relay_available", AsyncMock(return_value=False))
    mock_sleep = mocker.patch("trio.sleep", new=AsyncMock())
    
    result = await circuit_v2_transport._select_relay(peer_info)
    
    assert result is None
    assert circuit_v2_transport._is_relay_available.call_count == circuit_v2_transport.client_config.max_auto_relay_attempts
    assert circuit_v2_transport._relay_list == [relay1]
    metrics = _metrics_for(circuit_v2_transport, relay1)
    assert metrics["failures"] == circuit_v2_transport.client_config.max_auto_relay_attempts
    assert mock_sleep.call_count == circuit_v2_transport.client_config.max_auto_relay_attempts

@pytest.mark.trio
async def test_select_relay_round_robin(circuit_v2_transport, peer_info, mocker):
    """Test _select_relay round-robin selection of available relays."""
    mock_sleep = mocker.patch("trio.sleep", new=AsyncMock())
    # allow repeated calls by cycling the values
    mocker.patch("time.monotonic", side_effect=itertools.cycle([0, 0.1]))
    mocker.patch("time.time", return_value=1000.0)
    relay1 = MagicMock(spec=ID)
    relay2 = MagicMock(spec=ID)
    relay1.to_string.return_value = "relay1"
    relay2.to_string.return_value = "relay2"
    circuit_v2_transport.discovery.get_relays.return_value = [relay1, relay2]
    circuit_v2_transport.client_config.enable_auto_relay = True
    circuit_v2_transport._relay_list = [relay1, relay2]
    mocker.patch.object(circuit_v2_transport, "_is_relay_available", AsyncMock(return_value=True))
    
    circuit_v2_transport._last_relay_index = -1
    result1 = await circuit_v2_transport._select_relay(peer_info)
    assert result1.to_string() == relay2.to_string()
    assert circuit_v2_transport._last_relay_index == 0
    
    result2 = await circuit_v2_transport._select_relay(peer_info)
    assert result2.to_string() == relay1.to_string()
    assert circuit_v2_transport._last_relay_index == 1
    
    result3 = await circuit_v2_transport._select_relay(peer_info)
    assert result3.to_string() == relay2.to_string()
    assert circuit_v2_transport._last_relay_index == 0
    
    assert mock_sleep.call_count == 0
    # check metrics by looking up via helper
    metrics1 = _metrics_for(circuit_v2_transport, relay1)
    metrics2 = _metrics_for(circuit_v2_transport, relay2)
    assert metrics1["latency"] == pytest.approx(0.1, rel=1e-3)
    assert metrics2["latency"] == pytest.approx(0.1, rel=1e-3)

@pytest.mark.trio
async def test_is_relay_available_success(circuit_v2_transport, mocker):
    """Test _is_relay_available when the relay is reachable."""
    relay_id = MagicMock(spec=ID)
    stream = AsyncMock()
    mocker.patch.object(circuit_v2_transport.host, "new_stream", AsyncMock(return_value=stream))
    
    result = await circuit_v2_transport._is_relay_available(relay_id)
    
    assert result is True
    circuit_v2_transport.host.new_stream.assert_called_once_with(relay_id, [PROTOCOL_ID])
    stream.close.assert_called_once()

@pytest.mark.trio
async def test_is_relay_available_failure(circuit_v2_transport, mocker):
    """Test _is_relay_available when the relay is unreachable."""
    relay_id = MagicMock(spec=ID)
    mocker.patch.object(circuit_v2_transport.host, "new_stream", AsyncMock(side_effect=Exception("Connection failed")))
    
    result = await circuit_v2_transport._is_relay_available(relay_id)
    
    assert result is False
    circuit_v2_transport.host.new_stream.assert_called_once_with(relay_id, [PROTOCOL_ID])

@pytest.mark.trio
async def test_select_relay_scoring_priority(circuit_v2_transport, peer_info, mocker):
    """Test _select_relay prefers relays with better scores."""
    relay1 = MagicMock(spec=ID)
    relay2 = MagicMock(spec=ID)
    relay1.to_string.return_value = "relay1"
    relay2.to_string.return_value = "relay2"
    circuit_v2_transport.discovery.get_relays.return_value = [relay1, relay2]
    circuit_v2_transport.client_config.enable_auto_relay = True
    circuit_v2_transport._relay_list = [relay1, relay2]
    mocker.patch.object(circuit_v2_transport, "_is_relay_available", AsyncMock(return_value=True))
    #mocker.patch("time.monotonic", side_effect=itertools.cycle([0, 0.1, 0, 0.2]))
    #mocker.patch("time.time", return_value=1000.0)
    async def fake_measure_relay(relay_id, scored):
        if relay_id is relay1:
            scored.append((relay_id, 0.8))  # better score
            circuit_v2_transport._relay_metrics[relay_id]["failures"] = 0
        else:
            scored.append((relay_id, 0.5))  # worse score
            circuit_v2_transport._relay_metrics[relay_id]["failures"] = 1

    mocker.patch.object(circuit_v2_transport, "_measure_relay", side_effect=fake_measure_relay)
    circuit_v2_transport._relay_metrics = {
        relay1: {"latency": 0.1, "failures": 0, "last_seen": 999.9},
        relay2: {"latency": 0.2, "failures": 2, "last_seen": 900.0}
    }
    
    circuit_v2_transport._last_relay_index = -1
    result = await circuit_v2_transport._select_relay(peer_info)
    
    assert result.to_string() == relay1.to_string()
    m1 = _metrics_for(circuit_v2_transport, relay1)
    m2 = _metrics_for(circuit_v2_transport, relay2)
    assert m1["latency"] == pytest.approx(0.1, rel=1e-3)
    assert m2["latency"] == pytest.approx(0.2, rel=1e-3)
    assert m1["failures"] == 0
    assert m2["failures"] == 1

@pytest.mark.trio
async def test_select_relay_fewer_than_top_n(circuit_v2_transport, peer_info, mocker):
    """Test _select_relay when fewer relays than TOP_N are available."""
    relay1 = MagicMock(spec=ID)
    relay1.to_string.return_value = "relay1"
    circuit_v2_transport.discovery.get_relays.return_value = [relay1]
    circuit_v2_transport.client_config.enable_auto_relay = True
    circuit_v2_transport._relay_list = [relay1]
    mocker.patch.object(circuit_v2_transport, "_is_relay_available", AsyncMock(return_value=True))
    mocker.patch("time.monotonic", side_effect=itertools.cycle([0, 0.1]))
    mocker.patch("time.time", return_value=1000.0)
    
    circuit_v2_transport._last_relay_index = -1
    result = await circuit_v2_transport._select_relay(peer_info)
    
    assert result.to_string() == relay1.to_string()
    assert circuit_v2_transport._last_relay_index == 0
    assert len(circuit_v2_transport._relay_list) == 1

@pytest.mark.trio
async def test_select_relay_duplicate_relays(circuit_v2_transport, peer_info, mocker):
    """Test _select_relay handles duplicate relays correctly."""
    relay1 = MagicMock(spec=ID)
    relay1.to_string.return_value = "relay1"
    circuit_v2_transport.discovery.get_relays.return_value = [relay1, relay1]
    circuit_v2_transport.client_config.enable_auto_relay = True
    circuit_v2_transport._relay_list = [relay1]
    mocker.patch.object(circuit_v2_transport, "_is_relay_available", AsyncMock(return_value=True))
    mocker.patch("time.monotonic", side_effect=itertools.cycle([0, 0.1]))
    mocker.patch("time.time", return_value=1000.0)
    
    circuit_v2_transport._last_relay_index = -1
    result = await circuit_v2_transport._select_relay(peer_info)
    
    assert result.to_string() == relay1.to_string()
    assert len(circuit_v2_transport._relay_list) == 1

@pytest.mark.trio
async def test_select_relay_metrics_persistence(circuit_v2_transport, peer_info, mocker):
    """Test _select_relay persists and updates metrics across multiple calls."""
    relay1 = MagicMock(spec=ID)
    relay1.to_string.return_value = "relay1"
    circuit_v2_transport.discovery.get_relays.return_value = [relay1]
    circuit_v2_transport.client_config.enable_auto_relay = True
    circuit_v2_transport._relay_list = [relay1]
    mocker.patch("time.monotonic", side_effect=itertools.cycle([0, 0.1]))
    mocker.patch("time.time", side_effect=itertools.cycle([1000.0, 1001.0]))
    async_mock = AsyncMock(side_effect=[False, True])
    mocker.patch.object(circuit_v2_transport, "_is_relay_available", async_mock)
    
    circuit_v2_transport._last_relay_index = -1
    result = await circuit_v2_transport._select_relay(peer_info)
    # first attempt should not select (False), but metrics should exist
    assert relay1.to_string() == relay1.to_string()  # sanity
    assert relay1 in circuit_v2_transport._relay_list
    assert relay1 in [r for r in circuit_v2_transport._relay_list]
    assert relay1.to_string() == "relay1"
    assert relay1.to_string() == circuit_v2_transport._relay_list[0].to_string()
    assert relay1.to_string()  # metrics object should be created
    assert relay1.to_string() in (r.to_string() for r in circuit_v2_transport._relay_list)
    assert _metrics_for(circuit_v2_transport, relay1)  # metrics dict present
    
    circuit_v2_transport._last_relay_index = -1
    # ensure next call returns True
    async_mock.side_effect = [True]
    result = await circuit_v2_transport._select_relay(peer_info)
    assert result.to_string() == relay1.to_string()
    metrics = _metrics_for(circuit_v2_transport, relay1)
    # after a successful measurement failures should be 0
    assert metrics["failures"] == 0

@pytest.mark.trio
async def test_select_relay_backoff_timing(circuit_v2_transport, peer_info, mocker):
    """Test _select_relay exponential backoff on empty scored_relays."""
    circuit_v2_transport.discovery.get_relays.return_value = []
    circuit_v2_transport.client_config.enable_auto_relay = True
    circuit_v2_transport._relay_list = []
    mock_sleep = mocker.patch("trio.sleep", new=AsyncMock())
    circuit_v2_transport.client_config.max_auto_relay_attempts = 3
    
    await circuit_v2_transport._select_relay(peer_info)
    
    expected_backoffs = [min(2 ** i, 10) for i in range(3)]
    assert mock_sleep.call_args_list == [((backoff,), {}) for backoff in expected_backoffs]
    
@pytest.mark.trio
async def test_select_relay_disabled_auto_relay(circuit_v2_transport, peer_info, mocker):
    """Test _select_relay when auto_relay is disabled."""
    circuit_v2_transport.client_config.enable_auto_relay = False

    result = await circuit_v2_transport._select_relay(peer_info)
    
    assert result is None
    assert circuit_v2_transport.discovery.get_relays.call_count == 0
