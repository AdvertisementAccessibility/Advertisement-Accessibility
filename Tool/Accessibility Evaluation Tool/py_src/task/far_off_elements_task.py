import json
import logging
import os
from collections import defaultdict
from typing import Tuple
from PIL import Image
import imagehash
from GUI_utils import Node
from adb_utils import disable_talkback
from command import InfoCommand, InfoCommandResponse, JumpNextCommand, JumpPreviousCommand, PreviousCommand, \
    NextCommand, NavigateCommandResponse
from consts import BLIND_MONKEY_EVENTS_TAG, BLIND_MONKEY_TAG
from controller import Controller, TalkBackAPIController, TalkBackDirectionalController
from padb_utils import ParallelADBLogger
from results_utils import AddressBook, Actionables, capture_current_state
from snapshot import DeviceSnapshot
from task.snapshot_task import SnapshotTask
from utils import annotate_elements

logger = logging.getLogger(__name__)
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
class AnalyzeSnapshotActionTask(SnapshotTask):
    def __init__(self, snapshot: DeviceSnapshot,
                 check_both_directions: bool = False,
                 target_node: Node = None,
                 jump_mode: bool = False):
        if not isinstance(snapshot, DeviceSnapshot):
            raise Exception("TalkBack exploration requires a DeviceSnapshot!")
        super().__init__(snapshot)
        self.check_both_directions = check_both_directions
        self.target_node = target_node
        self.jump_mode = jump_mode

    async def is_target_node_found(self, padb_logger, focused_node: Node, controller: Controller, tags) -> Tuple[bool, str, str]:
        if self.target_node is None:
            return False, "", ""
        if self.target_node.same_identifiers(focused_node):
            return True, "", ""
        log_message_map, info_response = await padb_logger.execute_async_with_log(
            controller.execute(InfoCommand(question="is_focused", extra=self.target_node.toJSON())),
            tags=tags)
        android_logs = f"--------- First Focused Node -----------\n" \
                        f"{log_message_map[BLIND_MONKEY_TAG]}\n" \
                        f"----------------------------------------\n"
        android_event_logs = f"{log_message_map[BLIND_MONKEY_EVENTS_TAG]}\n"
        result = info_response.answer
        result = result.get('result', False) if result else False
        return (result, android_logs, android_event_logs)


    async def execute(self, required_actions: list = []) -> bool:
        snapshot: DeviceSnapshot = self.snapshot
        device = snapshot.device
        if not snapshot.address_book.audit_path_map[AddressBook.EXTRACT_ACTIONS].exists():
            logger.error("The actions should be extracted first!")
            return
        controller = TalkBackAPIController(device=device)
        await controller.setup()
        layout_path = self.snapshot.address_book.get_layout_path(mode=AddressBook.BASE_MODE,
                                                            index=AddressBook.INITIAL,
                                                            should_exists=True)
        element_num = len(self.snapshot.nodes)
        error_dict = defaultdict()
        if self.target_node is not None:
            logger.info(f"Looking for target node: {self.target_node}")
        padb_logger = ParallelADBLogger(snapshot.device)
        selected_nodes = []
        selected_nodes_copy = []
        with open(snapshot.address_book.extract_actions_nodes[Actionables.Selected]) as f:
            for line in f.readlines():
                node = Node.createNodeFromDict(json.loads(line.strip()))
                selected_nodes.append(node)
        selected_nodes_copy = selected_nodes
        if len(required_actions) != 0:
            selected_nodes = [selected_nodes[index] for index in required_actions]

        is_next = True
        all_nodes = {node.xpath: node for node in self.snapshot.get_nodes()}
        visited_node_xpaths_counter = defaultdict(int)
        self.snapshot.address_book.initiate_talkback_explore_task()
        visited_nodes = []
        none_node_count = 0
        last_screenshot = self.snapshot.initial_screenshot.resolve()
        screenshots = [last_screenshot]
        android_logs = ""
        android_event_logs = ""
        tags = [BLIND_MONKEY_TAG, BLIND_MONKEY_EVENTS_TAG]
        # --------------- Add the currently focused node to the visited nodes -----------------
        log_message_map, info_response = await padb_logger.execute_async_with_log(
            controller.execute(InfoCommand(question="a11y_focused")),
            tags=tags)
        android_logs += f"--------- First Focused Node -----------\n" \
                        f"{log_message_map[BLIND_MONKEY_TAG]}\n" \
                        f"----------------------------------------\n"
        android_event_logs += f"{log_message_map[BLIND_MONKEY_EVENTS_TAG]}\n"
        focused_node = None
        successful_result = self.target_node is None
        if info_response is not None and isinstance(info_response, InfoCommandResponse):
            node_dict = info_response.answer
            if node_dict is not None:
                focused_node = Node.createNodeFromDict(node_dict)
                if len(focused_node.xpath) > 0:
                    visited_nodes.append(focused_node)
                    visited_node_xpaths_counter[focused_node.xpath] += 1
        # -------------------------------------------------------------------------------------
        target_found_result_pack = await self.is_target_node_found(padb_logger=padb_logger, focused_node=focused_node, controller=controller, tags=tags)
        android_logs += target_found_result_pack[1]
        android_event_logs += target_found_result_pack[2]
        far_off_elements = []
        if self.target_node is not None and target_found_result_pack[0]:
            logger.info(f"The target node is found!")
            successful_result = True
        else:
            first_focused_node = selected_nodes_copy[0]
            first_focused_node.action = 'focus'
            log_message_map, navigate_response = await padb_logger.execute_async_with_log(
                controller.execute(first_focused_node, remove_after_read=True), tags=tags)
            counter = 0
            while len(selected_nodes) != 0:
                counter += 1
                if self.jump_mode:
                    command = JumpNextCommand() if is_next else JumpPreviousCommand()
                else:
                    command = NextCommand() if is_next else PreviousCommand()
                log_message_map, navigate_response = await padb_logger.execute_async_with_log(
                    controller.execute(command),
                    tags=tags)
                android_logs += f"--------------- Navigate ---------------\n" \
                                f"{log_message_map[BLIND_MONKEY_TAG]}\n" \
                                f"----------------------------------------\n"
                android_event_logs += f"{log_message_map[BLIND_MONKEY_EVENTS_TAG]}\n"
                # Check if the UI has changed
                if is_window_changed(log_message_map):
                    logger.info("Window Content Has Changed")
                    await capture_current_state(self.snapshot.address_book,
                                                                 self.snapshot.device,
                                                                 mode=AddressBook.FAROFF_MODE,
                                                                 index=len(screenshots),
                                                                 dumpsys=True,
                                                                 has_layout=True)
                    last_screenshot = self.snapshot.address_book.get_screenshot_path(AddressBook.FAROFF_MODE, len(screenshots))
                    last_screenshot = last_screenshot.resolve()
                    screenshots.append(last_screenshot)
                if navigate_response is None or not isinstance(navigate_response, NavigateCommandResponse):
                    logger.error("Terminate the exploration: Problem with navigation")
                    successful_result = False
                    break
                node = navigate_response.navigated_node
                if node is None or len(node.xpath) == 0:
                    none_node_count += 1
                    logger.warning(f"The visited node is None or does not have an xpath, none_node_count={none_node_count}."
                                   f"\n\tNode: {node}")
                    if none_node_count > 3:
                        logger.error(f"Terminate the exploration: none_node_count={none_node_count}")
                        successful_result = False
                        break
                target_found_result_pack = await self.is_target_node_found(padb_logger=padb_logger, focused_node=node, controller=controller, tags=tags)
                android_logs += target_found_result_pack[1]
                android_event_logs += target_found_result_pack[2]
                node_index = 0
                found_node = False
                for selected_node in selected_nodes:
                    if selected_node.xpath == node.xpath and counter > 10:
                            far_off_elements.append(selected_node)
                    elif selected_node.xpath == node.xpath:
                        logger.info("The number of swipe is: " + str(counter))
                        selected_node.action = 'click'
                        await capture_current_state(snapshot.address_book,
                                                    snapshot.device,
                                                    mode=AddressBook.INEFFECTIVE_MODE,
                                                    index=counter,
                                                    log_message_map=log_message_map,
                                                    dumpsys=True,
                                                    has_layout=True)
                        await padb_logger.execute_async_with_log(
                            controller.execute(selected_node),
                            tags=tags)
                        await capture_current_state(snapshot.address_book,
                                                    snapshot.device,
                                                    mode=AddressBook.INEFFECTIVE_MODE,
                                                    index=counter+1,
                                                    log_message_map=log_message_map,
                                                    dumpsys=True,
                                                    has_layout=True)
                        ineffective_action_path = self.snapshot.address_book.audit_path_map[AddressBook.INEFFECTIVE_MODE]
                        before_img = ineffective_action_path.joinpath(str(counter) + ".png")
                        after_img =  ineffective_action_path.joinpath(str(counter+1) + ".png")
                        hash_before = imagehash.average_hash(Image.open(before_img))
                        hash_after = imagehash.average_hash(Image.open(after_img))
                        cutoff = 10  # maximum bits that could be different between the hashes.
                        if abs(hash_before - hash_after) < cutoff:
                            ineffective_node = selected_node
                            annotate_elements(self.snapshot.initial_screenshot,
                                              self.snapshot.address_book.ineffective_screenshot,
                                              [ineffective_node])
                            error_dict['Ineffective Actions'] = round(1 / element_num, 4)
                        else:
                            error_dict['Ineffective Actions'] = round(0 / element_num, 4)
                        found_node = True
                        break
                    node_index += 1
                if found_node:
                    selected_nodes.remove(selected_nodes[node_index])
            swipe_result = defaultdict()
            if found_node:
                swipe_result['swipe_nums'] = counter
            with open(self.snapshot.address_book.swipe_num, 'w') as f:
                json.dump(swipe_result, f)
            if not snapshot.address_book.audit_path_map[AddressBook.UNLOCATABLE].exists():
                os.makedirs(snapshot.address_book.audit_path_map[AddressBook.UNLOCATABLE])
            if len(far_off_elements) != 0:
                annotate_elements(self.snapshot.initial_screenshot,
                                  self.snapshot.address_book.far_off_elements,
                                 far_off_elements)
            error_dict['Far-off Elements'] = round(len(far_off_elements) / element_num, 2)
            with open(self.snapshot.address_book.error_result, mode='a') as f:
                f.write(f"{json.dumps(error_dict)}\n")
            await disable_talkback()



