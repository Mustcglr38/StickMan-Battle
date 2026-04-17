import pickle
import socket
import struct
import threading
import time

import pygame


HOST = "127.0.0.1"
PORT = 5555
WINDOW_WIDTH = 960
WINDOW_HEIGHT = 540
GROUND_Y = 430
FPS = 60

GRAVITY = 1700
MOVE_SPEED = 280
JUMP_VELOCITY = -700
ATTACK_DURATION = 0.18
ATTACK_COOLDOWN = 0.35
PLAYER_HP = 100

SKY_COLOR = (146, 214, 255)
GROUND_COLOR = (93, 156, 89)
TEXT_COLOR = (30, 38, 56)
BAR_BG_COLOR = (55, 60, 70)
BAR_FILL_COLOR = (75, 220, 120)
BAR_EMPTY_COLOR = (200, 80, 80)
LOCAL_PLAYER_COLOR = (66, 135, 245)
REMOTE_PLAYER_COLOR = (230, 74, 96)

HEADER_STRUCT = struct.Struct("!I")


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
    # Her mesaj uzunluk basligi ile geldigi icin veri guvenli sekilde ayrisiyor.
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


class NetworkClient:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.snapshot_lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.latest_snapshot = {"players": {}}
        self.running = False
        self.player_id: int | None = None
        self.spawn = (220.0, float(GROUND_Y))
        self.receiver_thread: threading.Thread | None = None

    def connect(self) -> int:
        self.socket.connect((self.host, self.port))
        welcome_packet = recv_packet(self.socket)

        if not isinstance(welcome_packet, dict) or welcome_packet.get("type") != "welcome":
            raise ConnectionError("Server welcome packet could not be read.")

        self.player_id = int(welcome_packet["player_id"])
        self.spawn = tuple(welcome_packet.get("spawn", self.spawn))
        self.running = True
        self.receiver_thread = threading.Thread(target=self.receiver_loop, daemon=True)
        self.receiver_thread.start()
        return self.player_id

    def receiver_loop(self) -> None:
        try:
            while self.running:
                packet = recv_packet(self.socket)
                if packet is None:
                    break
                if isinstance(packet, dict) and packet.get("type") == "snapshot":
                    with self.snapshot_lock:
                        self.latest_snapshot = packet
        except (ConnectionError, EOFError, OSError, pickle.PickleError):
            pass
        finally:
            self.running = False

    def send_state(self, packet: dict) -> None:
        with self.send_lock:
            send_packet(self.socket, packet)

    def get_snapshot(self) -> dict:
        with self.snapshot_lock:
            return {
                "players": dict(self.latest_snapshot.get("players", {})),
                "server_time": self.latest_snapshot.get("server_time"),
            }

    def close(self) -> None:
        self.running = False
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.socket.close()
        except OSError:
            pass


class Player:
    def __init__(self, x: float, y: float) -> None:
        self.x = x
        self.y = y
        self.vx = 0.0
        self.vy = 0.0
        self.hp = PLAYER_HP
        self.facing = 1
        self.on_ground = True
        self.in_lobby = False
        self.animation_state = "idle_right"
        self.is_attacking = False
        self.attack_end_time = 0.0
        self.last_attack_time = -10.0
        self.previous_server_lobby_state = False

    def update(self, dt: float, move_left: bool, move_right: bool, jump_pressed: bool, attack_pressed: bool) -> None:
        now = time.monotonic()

        if self.in_lobby:
            self.vx = 0
            self.vy = 0
            self.on_ground = True
            self.is_attacking = False
            self.animation_state = "lobby_right"
            return

        direction = 0
        if move_left:
            direction -= 1
        if move_right:
            direction += 1

        self.vx = direction * MOVE_SPEED
        if direction < 0:
            self.facing = -1
        elif direction > 0:
            self.facing = 1

        if jump_pressed and self.on_ground:
            self.vy = JUMP_VELOCITY
            self.on_ground = False

        if attack_pressed and (now - self.last_attack_time) >= ATTACK_COOLDOWN:
            self.last_attack_time = now
            self.attack_end_time = now + ATTACK_DURATION
            self.is_attacking = True

        # Basit istemci tarafi fizik, server tarafindaki carpismayla birlikte calisir.
        if self.is_attacking and now >= self.attack_end_time:
            self.is_attacking = False

        self.vy += GRAVITY * dt
        self.x += self.vx * dt
        self.y += self.vy * dt

        self.x = clamp(self.x, 30, WINDOW_WIDTH - 30)
        if self.y >= GROUND_Y:
            self.y = GROUND_Y
            self.vy = 0
            self.on_ground = True
        else:
            self.on_ground = False

        facing_name = "right" if self.facing >= 0 else "left"
        if self.is_attacking:
            self.animation_state = f"attack_{facing_name}"
        elif not self.on_ground:
            self.animation_state = f"jump_{facing_name}"
        elif direction != 0:
            self.animation_state = f"run_{facing_name}"
        else:
            self.animation_state = f"idle_{facing_name}"

    def apply_server_state(self, server_state: dict | None) -> None:
        if not server_state:
            return

        was_in_lobby = self.in_lobby
        self.hp = int(server_state.get("hp", self.hp))
        self.in_lobby = bool(server_state.get("in_lobby", False))
        self.facing = int(server_state.get("facing", self.facing))
        server_x = float(server_state.get("x", self.x))
        server_y = float(server_state.get("y", self.y))

        if self.in_lobby or self.in_lobby != was_in_lobby or abs(server_x - self.x) > 150:
            self.x = server_x
            self.y = server_y
            self.vx = 0
            self.vy = 0

        if self.in_lobby:
            self.is_attacking = False
            self.on_ground = True

        self.previous_server_lobby_state = self.in_lobby

    def to_packet(self, ready_for_match: bool) -> dict:
        animation_state = "lobby_ready" if self.in_lobby and ready_for_match else self.animation_state
        return {
            "x": self.x,
            "y": self.y,
            "animation_state": animation_state,
            "is_attacking": self.is_attacking,
        }


class GameClient:
    def __init__(self, host: str = HOST, port: int = PORT) -> None:
        pygame.init()
        pygame.display.set_caption("Online Stickman Fighter")
        self.screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT))
        self.clock = pygame.time.Clock()
        self.title_font = pygame.font.SysFont("consolas", 30, bold=True)
        self.ui_font = pygame.font.SysFont("consolas", 20)
        self.network = NetworkClient(host, port)
        self.player_id = self.network.connect()
        self.player = Player(*self.network.spawn)
        self.running = True

    def run(self) -> None:
        try:
            while self.running:
                dt = self.clock.tick(FPS) / 1000.0
                snapshot = self.network.get_snapshot()
                local_state = snapshot["players"].get(self.player_id, {})
                self.player.apply_server_state(local_state)

                move_left = False
                move_right = False
                jump_pressed = False
                attack_pressed = False
                ready_for_match = False

                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        self.running = False
                    elif event.type == pygame.KEYDOWN:
                        if event.key == pygame.K_SPACE:
                            jump_pressed = True
                        elif event.key == pygame.K_f:
                            attack_pressed = True
                        elif event.key == pygame.K_RETURN:
                            ready_for_match = True
                    elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                        attack_pressed = True

                keys = pygame.key.get_pressed()
                move_left = keys[pygame.K_a]
                move_right = keys[pygame.K_d]

                self.player.update(dt, move_left, move_right, jump_pressed, attack_pressed)
                try:
                    self.network.send_state(self.player.to_packet(ready_for_match))
                except OSError:
                    self.running = False
                self.draw(snapshot)
        finally:
            self.network.close()
            pygame.quit()

    def draw(self, snapshot: dict) -> None:
        self.screen.fill(SKY_COLOR)
        pygame.draw.rect(self.screen, GROUND_COLOR, (0, GROUND_Y, WINDOW_WIDTH, WINDOW_HEIGHT - GROUND_Y))
        pygame.draw.line(self.screen, (70, 120, 70), (0, GROUND_Y), (WINDOW_WIDTH, GROUND_Y), 3)

        players = snapshot.get("players", {})
        remote_items = [(pid, data) for pid, data in players.items() if pid != self.player_id]
        remote_items.sort(key=lambda item: item[0])

        local_server_data = players.get(self.player_id)
        local_color = tuple(local_server_data.get("color", LOCAL_PLAYER_COLOR)) if local_server_data else LOCAL_PLAYER_COLOR
        self.draw_stickman(
            x=self.player.x,
            y=self.player.y,
            animation_state=self.player.animation_state,
            is_attacking=self.player.is_attacking,
            facing=self.player.facing,
            color=local_color,
        )

        for remote_id, data in remote_items:
            self.draw_stickman(
                x=float(data["x"]),
                y=float(data["y"]),
                animation_state=str(data["animation_state"]),
                is_attacking=bool(data["is_attacking"]),
                facing=int(data.get("facing", 1)),
                color=tuple(data.get("color", REMOTE_PLAYER_COLOR)),
            )
            if data.get("in_lobby"):
                self.draw_text("Lobby", float(data["x"]) - 28, float(data["y"]) - 105, self.ui_font)

        self.draw_health_bars(players)
        self.draw_hud_text(players)
        pygame.display.flip()

    def draw_stickman(
        self,
        x: float,
        y: float,
        animation_state: str,
        is_attacking: bool,
        facing: int,
        color: tuple[int, int, int],
    ) -> None:
        x = int(x)
        y = int(y)
        body_top = y - 58
        neck_y = y - 78
        head_center = (x, y - 94)
        hand_offset = 18

        if animation_state.startswith("run"):
            leg_swing = 10 if pygame.time.get_ticks() // 120 % 2 == 0 else -10
            arm_swing = -leg_swing
        elif animation_state.startswith("jump"):
            leg_swing = 8
            arm_swing = 8
        else:
            leg_swing = 0
            arm_swing = 0

        if is_attacking:
            attack_arm_x = x + (44 * facing)
            attack_arm_y = body_top - 4
            back_arm_x = x - (14 * facing)
            back_arm_y = body_top + 12
        else:
            attack_arm_x = x + hand_offset + arm_swing
            attack_arm_y = body_top + 8
            back_arm_x = x - hand_offset - arm_swing
            back_arm_y = body_top + 12

        front_leg_x = x + 12 + leg_swing
        back_leg_x = x - 12 - leg_swing

        pygame.draw.circle(self.screen, color, head_center, 14, 3)
        pygame.draw.line(self.screen, color, (x, neck_y), (x, body_top), 4)
        pygame.draw.line(self.screen, color, (x, body_top - 12), (attack_arm_x, attack_arm_y), 4)
        pygame.draw.line(self.screen, color, (x, body_top - 8), (back_arm_x, back_arm_y), 4)
        pygame.draw.line(self.screen, color, (x, body_top), (front_leg_x, y), 4)
        pygame.draw.line(self.screen, color, (x, body_top), (back_leg_x, y), 4)

    def draw_health_bars(self, players: dict) -> None:
        local_data = players.get(self.player_id, {})
        remote_items = [(pid, data) for pid, data in players.items() if pid != self.player_id]
        remote_items.sort(key=lambda item: item[0])
        opponent_data = remote_items[0][1] if remote_items else None

        self.draw_health_bar(20, 20, int(local_data.get("hp", self.player.hp)), "Player You", align_right=False)

        if opponent_data:
            self.draw_health_bar(
                WINDOW_WIDTH - 280,
                20,
                int(opponent_data.get("hp", PLAYER_HP)),
                f"Player {opponent_data['player_id']}",
                align_right=True,
            )
        else:
            self.draw_health_bar(WINDOW_WIDTH - 280, 20, 0, "Waiting...", align_right=True, active=False)

    def draw_health_bar(
        self,
        x: int,
        y: int,
        hp: int,
        label: str,
        align_right: bool = False,
        active: bool = True,
    ) -> None:
        width = 260
        height = 24
        hp_ratio = clamp(hp / PLAYER_HP if PLAYER_HP else 0, 0, 1)
        fill_width = int(width * hp_ratio)
        fill_color = BAR_FILL_COLOR if hp_ratio > 0.35 else BAR_EMPTY_COLOR

        pygame.draw.rect(self.screen, BAR_BG_COLOR, (x, y, width, height), border_radius=6)
        if active and fill_width > 0:
            pygame.draw.rect(self.screen, fill_color, (x, y, fill_width, height), border_radius=6)
        pygame.draw.rect(self.screen, (20, 20, 20), (x, y, width, height), 2, border_radius=6)

        label_surface = self.ui_font.render(f"{label}: {max(0, hp)} HP", True, TEXT_COLOR)
        label_rect = label_surface.get_rect()
        label_rect.top = y + 28
        if align_right:
            label_rect.right = x + width
        else:
            label_rect.left = x
        self.screen.blit(label_surface, label_rect)

    def draw_hud_text(self, players: dict) -> None:
        title = self.title_font.render("Stickman Arena", True, TEXT_COLOR)
        self.screen.blit(title, (WINDOW_WIDTH // 2 - title.get_width() // 2, 16))

        controls = "A/D: Move  Space: Jump  F / Left Click: Attack  Enter: Return From Lobby"
        controls_surface = self.ui_font.render(controls, True, TEXT_COLOR)
        self.screen.blit(controls_surface, (WINDOW_WIDTH // 2 - controls_surface.get_width() // 2, 58))

        if self.player.in_lobby:
            panel = pygame.Surface((420, 120), pygame.SRCALPHA)
            panel.fill((255, 255, 255, 185))
            self.screen.blit(panel, (WINDOW_WIDTH // 2 - 210, WINDOW_HEIGHT // 2 - 70))
            self.draw_text("Lobby", WINDOW_WIDTH // 2 - 40, WINDOW_HEIGHT // 2 - 40, self.title_font)
            self.draw_text("Canin bitti. Enter ile arenaya geri don.", WINDOW_WIDTH // 2 - 178, WINDOW_HEIGHT // 2 + 4, self.ui_font)

        online_count = len(players)
        self.draw_text(f"Online: {online_count}", 18, WINDOW_HEIGHT - 34, self.ui_font)

    def draw_text(self, text: str, x: float, y: float, font: pygame.font.Font) -> None:
        surface = font.render(text, True, TEXT_COLOR)
        self.screen.blit(surface, (x, y))


if __name__ == "__main__":
    GameClient().run()
