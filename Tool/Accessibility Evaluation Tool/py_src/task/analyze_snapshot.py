import json
import logging
import os
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from random import random

from GUI_utils import Node, NodesFactory
from adb_utils import read_local_android_file, disable_talkback, enable_talkback
from command import InfoCommand, ClickCommand, LocatableCommandResponse, InfoCommandResponse, NextCommand, \
    NavigateCommandResponse, FocusCommand
from consts import BLIND_MONKEY_EVENTS_TAG, BLIND_MONKEY_TAG, REGULAR_EXECUTE_TIMEOUT_TIME
from controller import TalkBackAPIController, TalkBackDirectionalController, TalkBackTouchController, A11yAPIController, \
    TouchController, Controller
from latte_executor_utils import report_atf_issues
from padb_utils import ParallelADBLogger
from results_utils import AddressBook, Actionables, capture_current_state
from snapshot import Snapshot, EmulatorSnapshot, DeviceSnapshot
from task.snapshot_task import SnapshotTask
from utils import annotate_elements, create_gif

logger = logging.getLogger(__name__)


def long_substr(data):
    substr = ''
    if len(data) > 1 and len(data[0]) > 0:
        for i in range(len(data[0])):
            for j in range(len(data[0]) - i + 1):
                if j > len(substr) and all(data[0][i:i + j] in x for x in data):
                    substr = data[0][i:i + j]
    elif len(data) == 1:
        substr = data[0]
    return substr


def is_window_changed(log_message_map):
    try:
        window_changed = False
        for line in log_message_map[BLIND_MONKEY_EVENTS_TAG].split("\n"):
            if 'WindowContentChange:' in line:
                change_part = line.split('WindowContentChange:')[1].strip()
                change_part = json.loads(change_part)
                if change_part['changedWindowId'] == change_part['activeWindowId']:
                    window_changed = True
                    break
        return window_changed
    except Exception as e:
        logger.error(f"Error in checking if the window is changed!:  {e}")
        return False


class AnalyzeSnapshotIssuesTask(SnapshotTask):
    def __init__(self, snapshot: EmulatorSnapshot):
        if not isinstance(snapshot, EmulatorSnapshot):
            raise Exception("Perform Actions task requires a EmulatorSnapshot!")
        super().__init__(snapshot)

    async def execute(self, ad_library: str, ad_type: str):
        snapshot: EmulatorSnapshot = self.snapshot
        device = snapshot.device
        if not snapshot.address_book.audit_path_map[AddressBook.EXTRACT_ACTIONS].exists():
            logger.error("The actions should be extracted first!")
            return
        snapshot.address_book.initiate_perform_actions_task()
        controllers = {
            'tb_touch': TalkBackTouchController(device=device),
            'tb_api': TalkBackAPIController(device=device),
            'a11y_api': A11yAPIController(device=device),
            'touch': TouchController(device=device)
        }
        padb_logger = ParallelADBLogger(device)
        await self.write_ATF_issues()
        selected_actionable_nodes = []
        random_integer = 0
        layout_path = self.snapshot.address_book.get_layout_path(mode=AddressBook.BASE_MODE,
                                                                 index=AddressBook.INITIAL,
                                                                 should_exists=True)
        element_num = len(self.snapshot.nodes)
        logger.info("element number is: " + str(element_num))
        error_dict = defaultdict()
        assertive_num = 0
        assertive_index = 0
        assertive_nodes = [node for node in self.snapshot.get_nodes() if node.live_region == 'ASSERTIVE']
        selected_nodes_copy = []
        if len(assertive_nodes) != 0:
            assertive_num += len(assertive_nodes)
            annotate_elements(self.snapshot.initial_screenshot,
                              self.snapshot.address_book.audit_path_map[AddressBook.UNLOCATABLE].joinpath(
                                  'assertive_nodes_' + str(assertive_index) + ".png"),
                              assertive_nodes)
        ineffective_node = []
        if ad_type == "Interstitial":
            ad_close_node = None
            with open(snapshot.address_book.extract_actions_nodes[Actionables.Selected]) as f:
                for line in f.readlines():
                    node = Node.createNodeFromDict(json.loads(line.strip()))
                    if "close" in node.text.lower() or node.content_desc.lower():
                        ad_close_node = node
                    selected_actionable_nodes.append(node)
            if ad_close_node == None:
                ad_close_node = selected_actionable_nodes[0]
        elif ad_type == "Native" or ad_type == "Banner":
            first_ad_index = 0
            first_ad_node = None
            rectangular_list = []
            if snapshot.address_book.single_ad_unit[2].exists():
                random_integer = random.randint(1, 2)
                with open(snapshot.address_book.single_ad_unit[random_integer]) as f:
                    for line in f.readlines():
                        node = Node.createNodeFromDict(json.loads(line.strip()))
                        rectangular_list.append(node.bounds)
                        if first_ad_index == 0:
                            first_ad_node = node
                            first_ad_index += 1
                        selected_actionable_nodes.append(node)
            else:
                with open(snapshot.address_book.single_ad_unit[0]) as f:
                    for line in f.readlines():
                        node = Node.createNodeFromDict(json.loads(line.strip()))
                        rectangular_list.append(','.join(map(str, node.bounds)))
                        if first_ad_index == 0:
                            first_ad_node = node
                            first_ad_index += 1
                        selected_actionable_nodes.append(node)
            largest_rect = await self.find_largest_rectangle(rectangular_list)
            with open(snapshot.address_book.extract_actions_nodes[Actionables.Selected]) as f:
                first_ad_index = 0
                for line in f.readlines():
                    node = Node.createNodeFromDict(json.loads(line.strip()))
                    if node.xpath == first_ad_node.xpath:
                        break
                    else:
                        first_ad_index += 1
            additional_nodes = []
            with open(snapshot.address_book.extract_actions_nodes[Actionables.Selected]) as f:
                for line in f.readlines():
                    node = Node.createNodeFromDict(json.loads(line.strip()))
                    additional_nodes.append(node)
            if first_ad_index != 0:
                additional_nodes = additional_nodes[first_ad_index - 1:first_ad_index]
            else:
                additional_nodes = [additional_nodes[len(additional_nodes) - 1]]
            additional_nodes.extend(selected_actionable_nodes)
            selected_nodes_copy = [x.xpath for x in selected_actionable_nodes]
            element_num, class_set = await self.count_ad_elements(selected_nodes_copy)
            selected_actionable_nodes = additional_nodes
            logger.info("element number is: " + str(element_num))

        logger.info(f"There are {len(selected_actionable_nodes)} actionable nodes!")
        tags = [BLIND_MONKEY_TAG, BLIND_MONKEY_EVENTS_TAG]
        controller_set = [controllers['tb_touch']]
        unlabelled_exploration = True
        for controller in controller_set:
            screenshot_to_visited_nodes = defaultdict(list)
            last_screenshot = self.snapshot.initial_screenshot.resolve()
            screenshots = [last_screenshot]
            await controller.setup()
            unlocatable_issues = []
            unvisited_nodes = []
            unlabelled_items = []
            for index, node in enumerate(selected_actionable_nodes):
                node.action = 'focus'
                # focus_command = FocusCommand(node)
                log_message_map, navigate_response = await padb_logger.execute_async_with_log(
                    controller.execute(node, remove_after_read=False), tags=tags)
                if is_window_changed(log_message_map):
                    # logger.info("Window Content Has Changed")
                    await capture_current_state(self.snapshot.address_book,
                                                self.snapshot.device,
                                                mode=AddressBook.UNLOCATABLE_MODE,
                                                index=len(screenshots),
                                                dumpsys=True,
                                                has_layout=True)
                    ################################################
                    last_layout_path = self.snapshot.address_book.get_layout_path(AddressBook.UNLOCATABLE_MODE,
                                                                                  len(screenshots))
                    with open(last_layout_path, encoding='utf-8') as f:
                        last_layout = f.read()
                    last_nodes = NodesFactory() \
                        .with_layout(last_layout) \
                        .with_xpath_pass() \
                        .with_ad_detection() \
                        .build()
                    newly_assertive_nodes = [node for node in last_nodes if node.live_region == 'ASSERTIVE']
                    if newly_assertive_nodes != assertive_nodes:
                        assertive_index += 1
                        if len(newly_assertive_nodes) != 0:
                            assertive_num += len(assertive_nodes)
                            annotate_elements(self.snapshot.initial_screenshot,
                                              self.snapshot.address_book.audit_path_map[
                                                  AddressBook.UNLOCATABLE].joinpath(
                                                  'assertive_nodes_' + str(assertive_index) + ".png"),
                                              newly_assertive_nodes)
                        assertive_nodes = newly_assertive_nodes
                    ###############################################
                    last_screenshot = self.snapshot.address_book.get_screenshot_path(AddressBook.UNLOCATABLE_MODE,
                                                                                     len(screenshots))
                    last_screenshot = last_screenshot.resolve()
                    screenshots.append(last_screenshot)

                result = await read_local_android_file(Controller.CONTROLLER_RESULT_FILE_NAME,
                                                       wait_time=REGULAR_EXECUTE_TIMEOUT_TIME)

                text_description = self.snapshot.get_text_description(node, depth=2)
                if len(text_description) == 0 and unlabelled_exploration and len(selected_nodes_copy) > 0:
                    for node_copy in selected_nodes_copy:
                        if node.xpath == node_copy:
                            unlabelled_items.append(node)
                elif len(selected_nodes_copy) == 0:
                    if len(text_description) == 0 and unlabelled_exploration:
                        unlabelled_items.append(node)
                json_result = json.loads(result)
                json_result['index'] = index

                if json_result['state'] == 'FAILED_LOCATE':
                    unlocatable_issues.append(json_result)
                    unvisited_nodes.append(node)
                else:
                    screenshot_to_visited_nodes[last_screenshot].append(node)

            if not snapshot.address_book.audit_path_map[AddressBook.UNLOCATABLE].exists():
                os.makedirs(snapshot.address_book.audit_path_map[AddressBook.UNLOCATABLE])

            if unlabelled_exploration:
                if len(unlabelled_items) != 0:
                    annotate_elements(self.snapshot.initial_screenshot,
                                      self.snapshot.address_book.unlabelled_screenshot,
                                      unlabelled_items)
                unlabelled_exploration = False

            if controller.mode() == 'tb_api':
                with open(snapshot.address_book.tb_api_unlocatable_result, 'w') as f:
                    for unlocatable_issue in unlocatable_issues:
                        f.write(f"{json.dumps(unlocatable_issue)}\n")

                if len(unvisited_nodes) != 0:
                    annotate_elements(self.snapshot.initial_screenshot,
                                      self.snapshot.address_book.tb_api_unvisited_nodes_screenshot,
                                      unvisited_nodes)

                create_gif(source_images=screenshots,
                           target_gif=self.snapshot.address_book.tb_api_unlocatable_gif,
                           image_to_nodes=screenshot_to_visited_nodes)
            else:
                with open(snapshot.address_book.tb_touch_unlocatable_result, "w") as f:
                    for unlocatable_issue in unlocatable_issues:
                        f.write(f"{json.dumps(unlocatable_issue)}\n")
                if len(unvisited_nodes) != 0:
                    annotate_elements(self.snapshot.initial_screenshot,
                                      self.snapshot.address_book.tb_touch_unvisited_nodes_screenshot,
                                      unvisited_nodes)

                create_gif(source_images=screenshots,
                           target_gif=self.snapshot.address_book.tb_touch_unlocatable_gif,
                           image_to_nodes=screenshot_to_visited_nodes)
            error_dict['Unlocatable Touch'] = round(len(unvisited_nodes) / element_num, 4)
            error_dict['Unlabelled Buttons'] = round(len(unlabelled_items) / element_num, 4)
            error_dict['Assertive Nodes'] = round(assertive_num / element_num, 4)

        controller = controllers['tb_api']
        log_message_map, info_response = await padb_logger.execute_async_with_log(
            controller.execute(InfoCommand(question="a11y_focused")),
            tags=tags)
        screenshot_to_visited_nodes = defaultdict(list)
        last_screenshot = self.snapshot.initial_screenshot.resolve()
        screenshots = [last_screenshot]
        await controller.setup()
        focused_node = None
        visited_nodes = []
        final_swipe_num = 0
        if ad_type != "Interstitial":
            node_index = 1
            focused_node = selected_actionable_nodes[node_index]
            focused_node.action = 'focus'
            swipe_num = 0
            if not self.is_rectangle_outside(largest_rect, focused_node.bounds):
                swipe_num=1
            log_message_map, navigate_response = await padb_logger.execute_async_with_log(
                controller.execute(focused_node, remove_after_read=True), tags=tags)
            action_response: LocatableCommandResponse = navigate_response
            while action_response.state != 'COMPLETED' and node_index < len(selected_actionable_nodes) - 1:
                node_index += 1
                focused_node = selected_actionable_nodes[node_index]
                focused_node.action = 'focus'
                log_message_map, navigate_response = await padb_logger.execute_async_with_log(
                    controller.execute(focused_node, remove_after_read=True), tags=tags)
                if not self.is_rectangle_outside(largest_rect, focused_node.bounds):
                    swipe_num += 1
                else:
                    final_swipe_num = swipe_num
                action_response: LocatableCommandResponse = navigate_response
            visited_nodes.append(focused_node)
            screenshot_to_visited_nodes[last_screenshot].append(focused_node)
        else:
            if info_response is not None and isinstance(info_response, InfoCommandResponse):
                node_dict = info_response.answer
                if node_dict is not None:
                    focused_node = Node.createNodeFromDict(node_dict)
                    if focused_node != None and len(focused_node.xpath) > 0:
                        visited_nodes.append(focused_node)
                        screenshot_to_visited_nodes[last_screenshot].append(focused_node)

        re_visit_nums = 0
        visted_count = 0
        while re_visit_nums < 2 and visted_count < 20:
            logger.info("Visited number is: " + str(visted_count))
            command = NextCommand()
            visted_count += 1
            log_message_map, navigate_response = await padb_logger.execute_async_with_log(
                controller.execute(command),
                tags=tags)
            if is_window_changed(log_message_map):
                await capture_current_state(self.snapshot.address_book,
                                            self.snapshot.device,
                                            mode=AddressBook.UNLOCATABLE_MODE,
                                            index=len(screenshots),
                                            dumpsys=True,
                                            has_layout=True)
                last_screenshot = self.snapshot.address_book.get_screenshot_path(AddressBook.UNLOCATABLE_MODE,
                                                                                 len(screenshots))
                last_screenshot = last_screenshot.resolve()
                screenshots.append(last_screenshot)
            if navigate_response is None or not isinstance(navigate_response, NavigateCommandResponse):
                logger.error("Terminate the exploration: Problem with navigation")
                successful_result = False
                break
            node = navigate_response.navigated_node
            while focused_node == None:
                command = NextCommand()
                log_message_map, navigate_response = await padb_logger.execute_async_with_log(
                    controller.execute(command),
                    tags=tags)
                if is_window_changed(log_message_map):
                    await capture_current_state(self.snapshot.address_book,
                                                self.snapshot.device,
                                                mode=AddressBook.UNLOCATABLE_MODE,
                                                index=len(screenshots),
                                                dumpsys=True,
                                                has_layout=True)
                    last_screenshot = self.snapshot.address_book.get_screenshot_path(
                        AddressBook.UNLOCATABLE_MODE,
                        len(screenshots))
                    last_screenshot = last_screenshot.resolve()
                    screenshots.append(last_screenshot)
                if navigate_response is None or not isinstance(navigate_response, NavigateCommandResponse):
                    logger.error("Terminate the exploration: Problem with navigation")
                    break
                focused_node = navigate_response.navigated_node
            if node != None:
                if node.xpath == focused_node.xpath:
                    re_visit_nums += 1
                if node not in screenshot_to_visited_nodes[last_screenshot]:
                    screenshot_to_visited_nodes[last_screenshot].append(node)
                for selected_node in selected_actionable_nodes:
                    if node.xpath == selected_node.xpath:
                        visited_nodes.append(selected_node)
        visited_nodes_set = set(visited_nodes)
        selected_nodes_set = set(selected_actionable_nodes[1:])
        unvisited_nodes = list(selected_nodes_set.difference(visited_nodes_set))
        if len(unvisited_nodes) != 0:
            annotate_elements(self.snapshot.initial_screenshot,
                              self.snapshot.address_book.tb_api_unvisited_nodes_screenshot,
                              unvisited_nodes)
        create_gif(source_images=screenshots,
                   target_gif=self.snapshot.address_book.tb_api_unlocatable_gif,
                   image_to_nodes=screenshot_to_visited_nodes)
        error_dict['Unlocatable Linear'] = round(len(unvisited_nodes) / element_num, 4)
        error_dict['Excessive Interaction'] = max(0, round(final_swipe_num - 14 / element_num, 4))
        error_dict['Ineffective Action'] = len(ineffective_node)
        error_dict['Ad Library'] = ad_library
        error_dict['Ad Type'] = ad_type
        with open(self.snapshot.address_book.error_result, mode='w') as f:
            f.write(f"{json.dumps(error_dict)}\n")
        await disable_talkback()

    async def write_ATF_issues(self):
        atf_issues = await report_atf_issues()
        logger.info(f"There are {len(atf_issues)} ATF issues in this screen!")
        with open(self.snapshot.address_book.perform_actions_atf_issues_path, "w") as f:
            for issue in atf_issues:
                f.write(json.dumps(issue) + "\n")
        annotate_elements(self.snapshot.initial_screenshot,
                          self.snapshot.address_book.perform_actions_atf_issues_screenshot,
                          atf_issues)

    async def count_ad_elements(self, node_xpaths: list):
        longest_substr = long_substr(node_xpaths)
        all_nodes = self.snapshot.nodes
        class_set = set()
        element_num = 0
        for all_node in all_nodes:
            if longest_substr in all_node.xpath:
                class_set.add(all_node.class_name)
                element_num += 1
        return element_num, class_set

    async def xml_to_str(self, xml_path: Path):
        tree = ET.parse(xml_path)
        root = tree.getroot()
        return ET.tostring(root, encoding='unicode', method='xml')

    async def find_largest_rectangle(self, rectangle_strings):
        if not rectangle_strings:
            return None

        # Convert string representations to tuples
        rectangles = []
        for rect_str in rectangle_strings:
            # Convert string to tuple
            rect = tuple(map(int, rect_str.strip("[]").split(',')))
            rectangles.append(rect)

        # Function to calculate the area of a rectangle
        def area(rect):
            x, y, width, height = rect
            return width * height

        # Find the rectangle with the maximum area
        largest_rectangle = max(rectangles, key=area)

        return largest_rectangle

    async def is_rectangle_outside(self,largest_rect, other_rect):
        """
        Check if other_rect is completely outside largest_rect.
        Rectangles are represented as (x, y, width, height).
        """

        # Calculate the right and bottom edges of the rectangles
        largest_right = largest_rect[0] + largest_rect[2]
        largest_bottom = largest_rect[1] + largest_rect[3]
        other_right = other_rect[0] + other_rect[2]
        other_bottom = other_rect[1] + other_rect[3]

        # Check if other_rect is outside largest_rect
        if other_rect[0] > largest_right or other_rect[1] > largest_bottom \
                or other_right < largest_rect[0] or other_bottom < largest_rect[1]:
            return True
        else:
            return False


