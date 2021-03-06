import errno
import os
import random
import signal
import socket
import threading
import time
from struct import pack, unpack

MAX_DATA_SIZE = 2 ** 15
DELAY_RATE = 0.0
LOSS_RATE = 0.01
CORRUPTION_RATE = 0.001
DUPLICATION_RATE = 0.0001

__rdt_stats = {
    'sent': 0,
    'timeout': 0,
    'ack': 0,
    'duplicated_ack': 0,
    'send_corrupt': 0,
    'send_unknown': 0,
    'received': 0,
    'corrupt': 0,
    'duplicated': 0,
    'unknown': 0,
    'safe': 0
}

__udt_stats = {
    'sent': 0,
    'delayed': 0,
    'lost': 0,
    'corrupt': 0,
    'duplicated': 0,
    'safe': 0,
    'received': 0
}


class __RDT:
    socket: socket.socket
    bound = False
    simulate_unreliability = False
    init = False
    send_seq_num = 0
    recv_seq_num = 0
    estimated_rtt = 1.0
    dev_rtt = 0


def rdt_init(address: tuple[str, int], bind=False, simulate_unreliability=False) -> None:
    __RDT.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    if bind:
        __RDT.socket.bind(address)
        __RDT.bound = True
    else:
        __RDT.socket.connect(address)

    __RDT.simulate_unreliability = simulate_unreliability
    __RDT.init = True


def rdt_send(data: bytes, address: tuple[str, int] = None) -> int:
    if not __RDT.init:
        raise OSError(errno.EDESTADDRREQ, os.strerror(errno.EDESTADDRREQ) + ': Call rdt_init')
    if __RDT.bound and address is None:
        raise OSError(errno.EDESTADDRREQ, os.strerror(errno.EDESTADDRREQ))
    elif len(data) > MAX_DATA_SIZE:
        raise OSError(errno.EMSGSIZE, os.strerror(errno.EMSGSIZE))

    send_pkt = __make_pkt(data, __RDT.send_seq_num)

    timeout_interval = max(1e-3, __RDT.estimated_rtt + 4 * __RDT.dev_rtt)
    has_timeout = False

    while True:
        size = __udt_send(send_pkt, address)
        start = time.perf_counter_ns()

        __rdt_stats['sent'] += 1

        try:
            __start_timer(timeout_interval)

            while True:
                recv_pkt, _ = __udt_recv()
                end = time.perf_counter_ns()

                _, flags = __extract(recv_pkt)

                if not __corrupt(recv_pkt):
                    if __is_ack(flags, __RDT.send_seq_num):
                        break
                    elif __is_ack(flags, __RDT.send_seq_num ^ 1):
                        __rdt_stats['duplicated_ack'] += 1
                    else:  # shouldn't happen
                        __rdt_stats['send_unknown'] += 1
                else:
                    __rdt_stats['send_corrupt'] += 1

            __stop_timer()
        except TimeoutError:
            __rdt_stats['timeout'] += 1

            timeout_interval *= 2
            has_timeout = True
        else:
            __rdt_stats['ack'] += 1

            __RDT.send_seq_num ^= 1

            if not has_timeout:
                sample_rtt = (end - start) / 1e9
                __RDT.dev_rtt = 0.75 * __RDT.dev_rtt + 0.25 * abs(__RDT.estimated_rtt - sample_rtt)
                __RDT.estimated_rtt = 0.875 * __RDT.estimated_rtt + 0.125 * sample_rtt

            return size
        finally:
            __stop_timer()


def rdt_recv() -> [tuple[bytes, tuple[str, int]], bytes]:
    if not __RDT.init:
        raise OSError(errno.EDESTADDRREQ, os.strerror(errno.EDESTADDRREQ) + ': Call rdt_init')

    while True:
        recv_pkt, address = __udt_recv()
        __rdt_stats['received'] += 1

        if not __corrupt(recv_pkt):
            data, flags = __extract(recv_pkt)

            if __has_seq(flags, __RDT.recv_seq_num):
                __rdt_stats['safe'] += 1

                send_pkt = __make_pkt(ack=True, seq_num=__RDT.recv_seq_num)
                __udt_send(send_pkt, address)

                __RDT.recv_seq_num ^= 1

                if __RDT.bound:
                    return data, address
                else:
                    return data
            elif __has_seq(flags, __RDT.recv_seq_num ^ 1):
                __rdt_stats['duplicated'] += 1
            else:  # shouldn't happen
                __rdt_stats['unknown'] += 1
        else:
            __rdt_stats['corrupt'] += 1

        send_pkt = __make_pkt(ack=True, seq_num=__RDT.recv_seq_num ^ 1)
        __udt_send(send_pkt, address)


def __make_pkt(data: bytes = b'', seq_num=0b1111, ack=False) -> bytes:
    flags = pack('!B', (seq_num << 4) + ack)
    checksum = pack('!H', __checksum(flags + data))

    return checksum + flags + data


def __extract(pkt: bytes) -> [bytes, int]:
    _, flags = unpack('!HB', pkt[:3])
    data = pkt[3:]

    return data, flags


def __checksum(packet: bytes) -> int:
    s = 0

    if len(packet) % 2 == 1:
        packet += b'\0'

    for i in range(0, len(packet), 2):
        c = s + int.from_bytes(packet[i:i + 2], 'big')
        s = (c & 0xffff) + (c >> 16)

    return ~s & 0xffff


def __corrupt(pkt: bytes) -> bool:
    expected_checksum = int.from_bytes(pkt[:2], 'big')
    checksum = __checksum(pkt[2:])

    return checksum != expected_checksum


def __is_ack(flags: int, seq_num: int) -> bool:
    return bool(flags & 0x01) and __has_seq(flags, seq_num)


def __has_seq(flags: int, seq_num: int) -> bool:
    return (flags >> 4) == seq_num


def __start_timer(timeout):
    signal.signal(signal.SIGALRM, lambda _, __: (_ for _ in ()).throw(TimeoutError()))
    signal.setitimer(signal.ITIMER_REAL, timeout)


def __stop_timer():
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, signal.SIG_IGN)


def rdt_stats(pprint=False):
    if not pprint:
        return {
            'rdt': __rdt_stats,
            'udt': __udt_stats
        }

    recv_p = (100 / __rdt_stats['received']) if __rdt_stats['received'] > 0 else 0
    sent_p = (100 / __rdt_stats['sent']) if __rdt_stats['sent'] > 0 else 0

    print('\nRDT stats:')
    print(f"* Received {__rdt_stats['received']} packets")

    if __rdt_stats['received'] > 0:
        print(f"  {__rdt_stats['safe']:6d} ({__rdt_stats['safe'] * recv_p:7.3f}%) safe packets")
        print(f"  {__rdt_stats['corrupt']:6d} ({__rdt_stats['corrupt'] * recv_p:7.3f}%) corrupt packets")
        print(f"  {__rdt_stats['duplicated']:6d} ({__rdt_stats['duplicated'] * recv_p:7.3f}%) duplicated packets")
        print(f"  {__rdt_stats['unknown']:6d} ({__rdt_stats['unknown'] * recv_p:7.3f}%) unknown packets")

    print(f"* Sent {__rdt_stats['sent']} packets")

    if __rdt_stats['sent'] > 0:
        print(f"  {__rdt_stats['ack']:6d} ({__rdt_stats['ack'] * sent_p:7.3f}%) ACK packets received")
        print(f"  {__rdt_stats['duplicated_ack']:6d} ({__rdt_stats['duplicated_ack'] * sent_p:7.3f}%) duplicated ACK packets received")
        print(f"  {__rdt_stats['send_corrupt']:6d} ({__rdt_stats['send_corrupt'] * sent_p:7.3f}%) corrupt packets received")
        print(f"  {__rdt_stats['send_unknown']:6d} ({__rdt_stats['send_unknown'] * sent_p:7.3f}%) unknown packets received")
        print(f"  {__rdt_stats['timeout']:6d} timeout events")

    udt_p = (100 / __udt_stats['sent']) if __udt_stats['sent'] > 0 else 0

    print('\nUDT stats:')
    print(f"* Received {__udt_stats['received']} packets")
    print(f"* Sent {__udt_stats['sent']} packets")

    if __RDT.simulate_unreliability and __udt_stats['sent'] > 0:
        print(f"  {__udt_stats['safe']:6d} ({__udt_stats['safe'] * udt_p:7.3f}%) safe packets")
        print(f"  {__udt_stats['delayed']:6d} ({__udt_stats['delayed'] * udt_p:7.3f}%) delayed packets")
        print(f"  {__udt_stats['lost']:6d} ({__udt_stats['lost'] * udt_p:7.3f}%) lost packets")
        print(f"  {__udt_stats['corrupt']:6d} ({__udt_stats['corrupt'] * udt_p:7.3f}%) corrupt packets")
        print(f"  {__udt_stats['duplicated']:6d} ({__udt_stats['duplicated'] * udt_p:7.3f}%) duplicated packets")


def __udt_send(pkt: bytes, address: tuple[str, int] = None, *, _is_recursion=False) -> int:
    __udt_stats['sent'] += 1 if not _is_recursion else 0

    if __RDT.simulate_unreliability and not _is_recursion:
        if random.random() < DELAY_RATE:
            __udt_stats['delayed'] += 1

            threading.Timer(1e-3, lambda: __udt_send(pkt, address, _is_recursion=True)).start()

            return len(pkt)
        elif random.random() < LOSS_RATE:
            __udt_stats['lost'] += 1

            return len(pkt)
        elif random.random() < CORRUPTION_RATE:
            __udt_stats['corrupt'] += 1

            mask = 0x00

            for bit in random.sample(range(8), random.randint(1, 2)):
                mask |= (1 << bit)

            pkt = bytearray(pkt)
            pkt[random.randrange(len(pkt))] ^= mask
            pkt = bytes(pkt)
        elif random.random() < DUPLICATION_RATE:
            __udt_stats['duplicated'] += 1

            __udt_send(pkt, address, _is_recursion=True)
        else:
            __udt_stats['safe'] += 1

    if address is not None:
        return __RDT.socket.sendto(pkt, address)
    else:
        return __RDT.socket.send(pkt)


def __udt_recv() -> tuple[bytes, tuple[str, int]]:
    data, address = __RDT.socket.recvfrom(MAX_DATA_SIZE)

    __udt_stats['received'] += 1

    return data, address


__all__ = ['rdt_init', 'rdt_send', 'rdt_recv', 'rdt_stats']
