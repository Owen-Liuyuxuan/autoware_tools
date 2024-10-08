#!/usr/bin/env python3

import importlib
import threading

import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy
from rclpy.qos import QoSHistoryPolicy
from rclpy.qos import QoSProfile
from rclpy.qos import QoSReliabilityPolicy


class TopicConnectionChecker(Node):
    def __init__(self):
        super().__init__("optimized_topic_connection_checker")
        self.get_logger().info("Optimized Topic Connection Checker node started")

        self.callback_group = ReentrantCallbackGroup()

        # List of important topics to check
        self.important_topics = [
            "/control/command/control_cmd",
            "/control/trajectory_follower/control_cmd",
            "/control/shift_decider/gear_cmd",
            "/planning/scenario_planning/trajectory",
            "/planning/turn_indicators_cmd",
            "/planning/mission_planning/route",
            "/perception/traffic_light_recognition/traffic_signals",
            "/perception/object_recognition/objects",
            # Add more important topics here
        ]

        self.ignore_topics = [
            "/rosout",
            "/parameter_events",
        ]

        self.topic_data = {}
        self.lock = threading.Lock()
        self.check_completed = threading.Event()
        self.topics_to_check_next_round = set()
        self.checked_topics = set()  # New set to keep track of checked topics
        self.reported_topics = set()  # New set to keep track of reported topics

        self.timer = None  # Timer object

        # Default QoS profile (used if unable to determine publisher's QoS)
        self.default_qos_profile = QoSProfile(
            durability=QoSDurabilityPolicy.VOLATILE,
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=1,
        )

    def check_topics(self):
        self.check_completed.clear()
        for topic in self.important_topics:
            if topic not in self.checked_topics:
                self.check_topic(topic)
                self.checked_topics.add(topic)

        # Set a timer for the overall check duration
        if self.timer:
            self.timer.cancel()  # Cancel existing timer if any
        self.timer = self.create_timer(5.0, self.finish_check, callback_group=self.callback_group)

        # Wait for the check to complete
        self.check_completed.wait()

        self.analyze_results()

    def check_topic(self, topic):
        msg_type = self.get_topic_type(topic)
        if not msg_type:
            self.get_logger().warn(f"Could not determine message type for topic: {topic}")
            return

        try:
            # Dynamically import the message type
            package_name, _, msg_name = msg_type.split("/")
            module = importlib.import_module(f"{package_name}.msg")
            msg_class = getattr(module, msg_name)
        except (ValueError, ImportError, AttributeError) as e:
            self.get_logger().error(f"Failed to import message type for topic {topic}: {e}")
            return

        self.topic_data[topic] = {"received": False, "publishers": [], "last_received": None}

        # Determine QoS profile
        qos_profile = self.get_publisher_qos(topic)

        # Create a subscription with the determined QoS profile
        self.create_subscription(
            msg_class,
            topic,
            lambda msg: self.topic_callback(topic, msg),
            qos_profile,
            callback_group=self.callback_group,
        )

    def get_publisher_qos(self, topic):
        publishers_info = self.get_publishers_info_by_topic(topic)
        if not publishers_info:
            # self.get_logger().error(f"No publishers found for topic {topic}. Using default QoS.")
            return self.default_qos_profile

        # Use the QoS of the first publisher (assuming all publishers use the same QoS)
        pub_qos = publishers_info[0].qos_profile

        return QoSProfile(
            durability=pub_qos.durability,
            reliability=pub_qos.reliability,
            history=pub_qos.history,
            depth=pub_qos.depth,
            lifespan=pub_qos.lifespan,
            deadline=pub_qos.deadline,
            liveliness=pub_qos.liveliness,
            liveliness_lease_duration=pub_qos.liveliness_lease_duration,
        )

    def topic_callback(self, topic, msg):
        with self.lock:
            self.topic_data[topic]["received"] = True
            self.topic_data[topic]["last_received"] = self.get_clock().now()

    def finish_check(self):
        # Get publisher information for all topics
        for topic in self.important_topics:
            publishers_info = self.get_publishers_info_by_topic(topic)
            with self.lock:
                self.topic_data[topic]["publishers"] = [
                    (p.node_name, p.node_namespace) for p in publishers_info
                ]

        self.check_completed.set()

    def get_topic_type(self, topic):
        topic_names_and_types = self.get_topic_names_and_types()
        for t, types in topic_names_and_types:
            if t == topic and types:
                return types[0]
        return None

    def analyze_results(self):
        stuck_topics = []
        for topic, data in self.topic_data.items():
            if not data["received"]:
                if (
                    topic not in self.ignore_topics
                    and len(data["publishers"]) == 1
                    and topic not in self.reported_topics
                ):
                    self.get_logger().warn(
                        f"Topic {topic} from {data['publishers'][0][0]} is stuck (no messages received)"
                    )
                    stuck_topics.append(topic)
                    self.reported_topics.add(topic)

            elif data["last_received"] is not None:
                # we assume that last received time is active because most of them are latch topics
                pass
            else:
                self.get_logger().warn(f"Topic {topic} has unexpected state")

        for topic in stuck_topics:
            self.analyze_topic_connections(topic)

        if self.topics_to_check_next_round:
            self.get_logger().info(
                f"Topics to check in the next round: {self.topics_to_check_next_round}"
            )
            self.important_topics = list(self.topics_to_check_next_round)
            self.topics_to_check_next_round.clear()
            self.check_topics()  # Start another round of checks

    def analyze_topic_connections(self, stuck_topic):
        publishers = self.topic_data[stuck_topic]["publishers"]
        for node_name, node_namespace in publishers:
            # Get all topics this node is subscribing to
            node_subscriptions = self.get_subscriber_names_and_types_by_node(
                node_name, node_namespace
            )

            for topic, types in node_subscriptions:
                if topic in self.ignore_topics:
                    continue
                publishers_info = self.get_publishers_info_by_topic(topic)
                if len(publishers_info) == 0:
                    self.get_logger().error(
                        f"  Node {node_name} is subscribing to topic {topic} but there are no publishers"
                    )
                elif len(publishers_info) > 2:
                    # topics with multiple publishers are usually debuggers or loggers and are not a problem
                    continue
                if topic in self.topic_data:
                    status = "stuck" if not self.topic_data[topic]["received"] else "active"
                    self.get_logger().debug(f"  Subscribed topic {topic} is {status}")
                elif topic not in self.checked_topics:
                    self.get_logger().debug(f"  Subscribed topic {topic} was not checked")
                    self.topics_to_check_next_round.add(topic)


def main(args=None):
    rclpy.init(args=args)
    checker = TopicConnectionChecker()

    executor = MultiThreadedExecutor()
    executor.add_node(checker)

    thread = threading.Thread(target=executor.spin, daemon=True)
    thread.start()

    try:
        checker.check_topics()
    except KeyboardInterrupt:
        pass
    finally:
        rclpy.shutdown()
        thread.join()


if __name__ == "__main__":
    main()
