import json
import logging
from results_utils import AddressBook
from snapshot import EmulatorSnapshot
from task.analyze_snapshot import AnalyzeSnapshotIssuesTask
from task.extract_actions_task import ExtractActionsTask
from task.snapshot_task import SnapshotTask

logger = logging.getLogger(__name__)
class AnalyzeAdScreenTask(SnapshotTask):
    def __init__(self, snapshot: EmulatorSnapshot):
        if not isinstance(snapshot, EmulatorSnapshot):
            raise Exception("This task requires a UISnapshot!")
        super().__init__(snapshot)

    async def execute(self, har_path:str):
        snapshot: EmulatorSnapshot = self.snapshot
        device = snapshot.device
        ad_library, ad_type = await find_first_ad_library_and_format(har_path)
        await ExtractActionsTask(snapshot).execute()
        await AnalyzeSnapshotIssuesTask(snapshot).execute(ad_library=ad_library, ad_type=ad_type)
        if not snapshot.address_book.audit_path_map[AddressBook.EXTRACT_ACTIONS].exists():
            logger.error("The actions should be extracted first!")
            return
async def find_first_ad_library_and_format(har_file_path):
    # Load the HAR file
    with open(har_file_path, 'r') as file:
        data = json.load(file)

    # Iterate through the entries in the HAR file
    for entry in data['log']['entries']:
        request_url = entry['request']['url']

        # Extract requestBody for Meta Audience
        post_data = entry['request'].get('postData', {}).get('text', '')

        # Check for Meta Audience conditions in the post data
        if 'https://graph.facebook.com/network_ads_common' in request_url:
            if 'PLACEMENT_TYPE=native' in post_data:
                return 'Meta Audience', 'Native'
            elif 'PLACEMENT_TYPE=banner' in post_data:
                return 'Meta Audience', 'Banner'
            elif 'interstitial' in post_data or 'rewarded' in post_data:
                return 'Meta Audience', 'Interstitial'

        # Extract headers for AppLovin
        headers = {header['name']: header['value'] for header in entry['request'].get('headers', [])}

        # Check for AppLovin conditions in the headers
        if 'https://ms4.applovin.com/1.0/mediate' in request_url:
            applovin_ad_format = headers.get('applovin-ad-format', '').upper()
            if applovin_ad_format == 'NATIVE':
                return 'AppLovin', 'Native'
            elif applovin_ad_format in ['INTER', 'REWARDED', 'APPOPEN']:
                return 'AppLovin', 'Interstitial'
            elif applovin_ad_format in ['BANNER', 'MREC']:
                return 'AppLovin', 'Banner'

        # Check for AdMob conditions
        query_params = {param['name']: param['value'] for param in entry['request'].get('queryString', [])}
        if 'url' in query_params:
            query_url = query_params['url']
            ad_format = None
            if 'format=379x59_as' in query_url or 'format=320x50_mb' in query_url:
                ad_format = 'Banner'
            elif 'format=interstitial_mb' in query_url:
                ad_format = 'Interstitial'
            elif 'native_version' in query_url:
                ad_format = 'Native'

            if 'https://pagead2.googlesyndication.com/pcs/activeview' in request_url and ad_format:
                return 'AdMob', ad_format

    return None, None

