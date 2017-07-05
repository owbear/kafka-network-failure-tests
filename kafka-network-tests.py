import json
import re
from subprocess import check_call, check_output, Popen, PIPE
import time
from docker import Client


RUNCLASS_PATH = "/tmp/kafka_2.12-0.10.2.1/bin/kafka-run-class.sh"
TEST_TOPIC = "test-topic"


def zk_query(path, fail_on_error=False):
    zk_cmd = [RUNCLASS_PATH, "kafka.tools.ZooKeeperMainWrapper", "get", path]
    output, errors = Popen(zk_cmd, universal_newlines=True, stdout=PIPE, stderr=PIPE).communicate()
    for line in output.split():
        match = re.match("^({.+})$", line)
        if match:
            return json.loads(match.group(0))
    if fail_on_error:
        raise RuntimeError("Command '{}' failed with: {}".format(zk_cmd, errors))


def remove_all_docker_containers():
    containers = check_output("docker ps -a -q --filter label=com.docker.compose.project".split()).decode().split()
    if containers:
        check_call("docker rm -f".split() + containers)


def log_in_utc(str):
    import datetime
    date = datetime.datetime.utcnow().strftime("%a %b %d %H:%M:%S UTC %Y")
    print("{}: {}".format(date, str))


def docker_compose(cmd):
    check_call(["docker-compose", "--project-name", "kafkanetworkfailuretests"] + cmd.split())


def change_isr(broker_ids):
    import tempfile
    with tempfile.NamedTemporaryFile() as f:
        reassignment = '{"partitions":[{"topic":"%s", "partition":0, "replicas":%s}], "version":2 }' % (
            TEST_TOPIC, broker_ids)
        f.write(reassignment.encode())
        f.flush()
        log_in_utc("# Reassigning partitions using: " + reassignment)
        reassign_cmd = "--zookeeper localhost:2181 --reassignment-json-file %s" % f.name
        check_call([RUNCLASS_PATH, "kafka.admin.ReassignPartitionsCommand", "--execute"] + reassign_cmd.split())
        check_call([RUNCLASS_PATH, "kafka.admin.ReassignPartitionsCommand", "--verify"] + reassign_cmd.split())


def get_docker_id(kafka_id):
    containers = Client.from_env().containers(filters={'label': 'com.docker.compose.project'})
    broker_port = zk_query("/brokers/ids/%d" % kafka_id)['port']
    for c in containers:
        if not c['Ports'] or 'PublicPort' not in c['Ports'][0]:
            pass  # Skip zookeeper, multiple ports, not all of them public
    return [c['Id'] for c in containers if c['Ports'] and c['Ports'][0].get('PublicPort') == broker_port][0]


class Cluster(object):
    class Node(object):
        def __init__(self, kafka_id):
            self.kafka_id = kafka_id
            self.docker_id = get_docker_id(kafka_id)[:12]

        def __repr__(self):
            return "{} ({})".format(self.kafka_id, self.docker_id)

    def __init__(self, logger):
        self.logger = logger
        self.state = None

        logger("# Wait for the cluster to start")
        for _ in range(7):
            time.sleep(1)
            self.state = zk_query("/brokers/topics/%s/partitions/0/state" % TEST_TOPIC, fail_on_error=False)
            if self.state and len(self.state.get('isr', [])) > 1:
                logger(self.state)
                break

        self.controller = Cluster.Node(zk_query("/controller")['brokerid'])
        logger("# Kafka cluster has controller {} ({})".format(self.controller.kafka_id, self.controller.docker_id))
        self.leader = Cluster.Node(self.state['leader'])
        self.isrs = [Cluster.Node(isr) for isr in self.state['isr'] if isr != self.state['leader']]
        logger("# Topic {} has leader: {} and isr(s): {}".format(TEST_TOPIC, self.leader, self.isrs))

        for isr in self.isrs:
            assert self.leader.kafka_id != isr.kafka_id


def start_cluster():
    log_in_utc("# Remove all docker containers for a clean start")
    remove_all_docker_containers()
    log_in_utc("# Start zookeeper and 3 kafka instances")
    docker_compose("up -d --scale kafka=3 kafka")


def do_test_producing_to_lost_leader(producer, consumer, take_down):
    """ Start a cluster, let the producer produce for a while, bring down the topic leader and read the complete backlog
    to see if the producer failed to produce data to the cluster for a prolonged time"""

    start_cluster()

    cluster = Cluster(log_in_utc)

    log_in_utc("# Start a producer and let it run for a while")
    docker_compose("up -d --scale kafka=3 kafka %s" % producer)
    time.sleep(10)

    if take_down:
        take_down(cluster.leader.docker_id)
    else:
        change_isr([cluster.isrs[0].kafka_id])

    log_in_utc("# Sleep for a while with the leader disconnected before checking what the producer has produced")
    for _ in range(20):
        log_in_utc(zk_query("/brokers/topics/%s/partitions/0/state" % TEST_TOPIC))
        time.sleep(2)

    log_in_utc("# Stop the producer")
    docker_compose("stop --timeout 1 %s" % producer)

    log_in_utc("# Start the consumer")
    docker_compose("up -d --scale kafka=3 kafka %s" % consumer)

    log_in_utc("# Wait for 3 minutes for the consumer to consume (it can take even longer)")
    time.sleep(180)

    log_in_utc("# Stop the consumer")
    docker_compose("stop %s" % consumer)

    log_in_utc("# Logs of what the producer produced and consumer consumed")
    docker_compose("logs --timestamps %s" % consumer)
    docker_compose("logs --timestamps %s" % producer)
    check_call(["docker", "logs", cluster.isrs[0].docker_id])


def take_down_ifdown(docker_id):
    log_in_utc("# Bring down eth0 on {}".format(docker_id))
    check_call("docker exec --privileged -t {} ifconfig eth0 down".format(docker_id).split())


def take_down_disconnect(docker_id):
    log_in_utc("# Disconnect {} from network".format(docker_id))
    Client.from_env().disconnect_container_from_network(docker_id, "kafkanetworkfailuretests_default", force=True)


def take_down_kill(docker_id):
    log_in_utc("# Kill -9 {}".format(docker_id))
    Client.from_env().remove_container(docker_id, force=True)


#######
# Tests


def test_producing_to_lost_leader_using_librdkafka_producer_and_ifdown():
    do_test_producing_to_lost_leader("producer_librdkafka", "consumer_java", take_down_ifdown)


def test_producing_to_lost_leader_using_librdkafka_producer_and_disconnect():
    do_test_producing_to_lost_leader("producer_librdkafka", "consumer_java", take_down_disconnect)


def test_producing_to_lost_leader_using_librdkafka_producer_and_kill():
    do_test_producing_to_lost_leader("producer_librdkafka", "consumer_java", take_down_kill)


def test_producing_to_lost_leader_using_librdkafka_producer_and_change_isr():
    do_test_producing_to_lost_leader("producer_librdkafka", "consumer_java", None)


def test_producing_to_lost_leader_using_java_producer_and_ifdown():
    do_test_producing_to_lost_leader("producer_java", "consumer_java", take_down_ifdown)


def test_producing_to_lost_leader_using_java_producer_and_disconnect():
    do_test_producing_to_lost_leader("producer_java", "consumer_java", take_down_disconnect)


def test_producing_to_lost_leader_using_java_producer_and_kill():
    do_test_producing_to_lost_leader("producer_java", "consumer_java", take_down_kill)


def test_producing_to_lost_leader_using_java_producer_and_change_isr():
    do_test_producing_to_lost_leader("producer_java", "consumer_java", None)
