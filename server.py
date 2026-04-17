import pickle
import socket
import struct
import threading
import time
from dataclasses import dataclass, field


HOST = "127.0.0.1"
PORT = 5555
TICK_RATE = 60

ARENA_WIDTH = 960
ARENA_HEIGHT = 540
GROUND_Y = 430
PLAYER_HP = 100
ATTACK_DAMAGE = 15
ATTACK_DURATION = 0.18
ATTACK_COOLDOWN = 0.35
ATTACK_RANGE = 60
BODY_HEIGHT = 72

HEADER_STRUCT = struct.Struct("!I")
PLAYER_COLORS = [
    (66, 135, 245),
    (230, 74, 96),
    (255, 170, 66),
    (102, 187, 106),
]


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def recv_exact(sock: socket.socket, size: int) -> bytes | None:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            return None
        data.extend(chunk)
    return bytes(data)


def recv_packet(sock: socket.socket):
    # TCP akisinda mesaj siniri olmadigi icin once paket boyutunu okuyoruz.
    header = recv_exact(sock, HEADER_STRUCT.size)
    if header is None:
        return None

    (payload_size,) = HEADER_STRUCT.unpack(header)
    payload = recv_exact(sock, payload_size)
    if payload is None:
        return None
    return pickle.loads(payload)


def send_packet(sock: socket.socket, payload) -> None:
    raw = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
    sock.sendall(HEADER_STRUCT.pack(len(raw)) + raw)


@dataclass
class ClientConnection:
    player_id: int
    sock: socket.socket
    address: tuple[str, int]
    send_lock: threading.Lock = field(default_factory=threading.Lock)

    def send(self, payload) -> None:
        with self.send_lock:
            send_packet(self.sock, payload)


@dataclass
class ServerPlayer:
    player_id: int
    x: float
    y: float
    color: tuple[int, int, int]
    animation_state: str = "idle_right"
    is_attacking: bool = False
    hp: int = PLAYER_HP
    in_lobby: bool = False
    facing: int = 1
    attack_started_at: float = -10.0
    last_attack_at: float = -10.0
    hit_targets: set[int] = field(default_factory=set)

    def to_dict(self) -> dict:
        return {
            "player_id": self.player_id,
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "hp": self.hp,
            "color": self.color,
            "animation_state": self.animation_state,
            "is_attacking": self.is_attacking,
            "in_lobby": self.in_lobby,
            "facing": self.facing,
        }


class GameServer:
    def __init__(self, host: str = HOST, port: int = PORT) -> None:
        self.host = host
        self.port = port
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.players: dict[int, ServerPlayer] = {}
        self.clients: dict[int, ClientConnection] = {}
        self.lock = threading.Lock()
        self.running = True
        self.next_player_id = 1

    def create_spawn_position(self, player_id: int) -> tuple[float, float]:
        column = (player_id - 1) % 2
        row = (player_id - 1) // 2
        x = 260 + (column * 440)
        x += (row % 2) * 40
        return float(x), float(GROUND_Y)

    def create_lobby_position(self, player_id: int) -> tuple[float, float]:
        x = 100 + ((player_id - 1) % 6) * 130
        return float(x), float(GROUND_Y)

    def allocate_player(self) -> ServerPlayer:
        player_id = self.next_player_id
        self.next_player_id += 1
        spawn_x, spawn_y = self.create_spawn_position(player_id)
        color = PLAYER_COLORS[(player_id - 1) % len(PLAYER_COLORS)]
        player = ServerPlayer(player_id=player_id, x=spawn_x, y=spawn_y, color=color)
        self.players[player_id] = player
        return player

    def start(self) -> None:
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen()
        print(f"Server listening on {self.host}:{self.port}")

        broadcaster = threading.Thread(target=self.broadcast_loop, daemon=True)
        broadcaster.start()

        try:
            while self.running:
                client_socket, address = self.server_socket.accept()
                client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                with self.lock:
                    player = self.allocate_player()
                    self.clients[player.player_id] = ClientConnection(
                        player_id=player.player_id,
                        sock=client_socket,
                        address=address,
                    )

                self.send_welcome(player.player_id)
                print(f"Player {player.player_id} connected from {address}")

                thread = threading.Thread(
                    target=self.handle_client,
                    args=(player.player_id,),
                    daemon=True,
                )
                thread.start()
        except KeyboardInterrupt:
            print("\nServer shutting down...")
        finally:
            self.shutdown()

    def send_welcome(self, player_id: int) -> None:
        connection = self.clients[player_id]
        player = self.players[player_id]
        connection.send(
            {
                "type": "welcome",
                "player_id": player_id,
                "spawn": (player.x, player.y),
                "arena_size": (ARENA_WIDTH, ARENA_HEIGHT),
                "ground_y": GROUND_Y,
            }
        )

    def shutdown(self) -> None:
        self.running = False
        with self.lock:
            clients = list(self.clients.values())
            self.clients.clear()
            self.players.clear()

        for connection in clients:
            try:
                connection.sock.close()
            except OSError:
                pass

        try:
            self.server_socket.close()
        except OSError:
            pass

    def handle_client(self, player_id: int) -> None:
        connection = self.clients.get(player_id)
        if connection is None:
            return
        sock = connection.sock

        try:
            while self.running:
                packet = recv_packet(sock)
                if packet is None:
                    break
                self.process_player_packet(player_id, packet)
        except (ConnectionError, EOFError, OSError, pickle.PickleError):
            pass
        finally:
            self.disconnect_player(player_id)

    def process_player_packet(self, player_id: int, packet: dict) -> None:
        required_keys = {"x", "y", "animation_state", "is_attacking"}
        if not isinstance(packet, dict) or not required_keys.issubset(packet):
            return

        with self.lock:
            player = self.players.get(player_id)
            if player is None:
                return

            animation_state = str(packet["animation_state"])
            incoming_x = float(packet["x"])
            incoming_y = float(packet["y"])
            wants_attack = bool(packet["is_attacking"])
            now = time.monotonic()

            if animation_state.endswith("_left"):
                player.facing = -1
            elif animation_state.endswith("_right"):
                player.facing = 1

            if player.in_lobby:
                if animation_state == "lobby_ready":
                    self.respawn_player(player)
                return

            player.x = clamp(incoming_x, 30, ARENA_WIDTH - 30)
            player.y = clamp(incoming_y, 120, GROUND_Y)
            player.animation_state = animation_state

            if wants_attack and (now - player.last_attack_at) >= ATTACK_COOLDOWN:
                player.attack_started_at = now
                player.last_attack_at = now
                player.hit_targets.clear()

    def respawn_player(self, player: ServerPlayer) -> None:
        player.x, player.y = self.create_spawn_position(player.player_id)
        player.hp = PLAYER_HP
        player.in_lobby = False
        player.is_attacking = False
        player.animation_state = "idle_right" if player.facing >= 0 else "idle_left"
        player.attack_started_at = -10.0
        player.last_attack_at = time.monotonic()
        player.hit_targets.clear()

    def send_player_to_lobby(self, player: ServerPlayer) -> None:
        player.in_lobby = True
        player.hp = 0
        player.is_attacking = False
        player.attack_started_at = -10.0
        player.animation_state = "lobby_right"
        player.x, player.y = self.create_lobby_position(player.player_id)
        player.hit_targets.clear()

    def build_attack_rect(self, player: ServerPlayer) -> tuple[float, float, float, float]:
        body_top = player.y - BODY_HEIGHT
        body_bottom = player.y - 18
        if player.facing >= 0:
            left = player.x + 10
            right = player.x + ATTACK_RANGE
        else:
            left = player.x - ATTACK_RANGE
            right = player.x - 10
        return left, body_top, right, body_bottom

    def point_in_rect(self, x: float, y: float, rect: tuple[float, float, float, float]) -> bool:
        left, top, right, bottom = rect
        return left <= x <= right and top <= y <= bottom

    def update_combat(self) -> None:
        now = time.monotonic()
        players = list(self.players.values())

        for player in players:
            # Saldiriyi tek vurusluk pencere halinde aktif tutuyoruz.
            player.is_attacking = not player.in_lobby and (now - player.attack_started_at) <= ATTACK_DURATION

        for attacker in players:
            if not attacker.is_attacking:
                continue

            hitbox = self.build_attack_rect(attacker)

            for target in players:
                if target.player_id == attacker.player_id:
                    continue
                if target.in_lobby or target.hp <= 0:
                    continue
                if target.player_id in attacker.hit_targets:
                    continue

                target_body_x = target.x
                target_body_y = target.y - 44
                if self.point_in_rect(target_body_x, target_body_y, hitbox):
                    target.hp = max(0, target.hp - ATTACK_DAMAGE)
                    attacker.hit_targets.add(target.player_id)
                    if target.hp <= 0:
                        self.send_player_to_lobby(target)

    def build_snapshot(self) -> dict:
        return {
            "type": "snapshot",
            "server_time": time.time(),
            "players": {player_id: player.to_dict() for player_id, player in self.players.items()},
        }

    def broadcast_loop(self) -> None:
        frame_time = 1.0 / TICK_RATE

        while self.running:
            start_time = time.monotonic()
            disconnected_ids: list[int] = []

            with self.lock:
                self.update_combat()
                snapshot = self.build_snapshot()
                client_items = list(self.clients.items())

            for player_id, connection in client_items:
                try:
                    connection.send(snapshot)
                except OSError:
                    disconnected_ids.append(player_id)

            for player_id in disconnected_ids:
                self.disconnect_player(player_id)

            elapsed = time.monotonic() - start_time
            sleep_time = frame_time - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    def disconnect_player(self, player_id: int) -> None:
        with self.lock:
            connection = self.clients.pop(player_id, None)
            player = self.players.pop(player_id, None)

        if connection is not None:
            try:
                connection.sock.close()
            except OSError:
                pass

        if player is not None:
            print(f"Player {player_id} disconnected")


if __name__ == "__main__":
    GameServer().start()
