import requests
from datetime import datetime
from data_processing import format_duration
from auth import is_authenticated, refresh_token_if_needed

def get_ga4_properties(session):
    if not is_authenticated() or not refresh_token_if_needed():
        return []

    try:
        headers = {
            'Authorization': f'Bearer {session["access_token"]}',
            'Content-Type': 'application/json'
        }

        response = requests.get(
            'https://analyticsadmin.googleapis.com/v1beta/accountSummaries',
            headers=headers
        )
        response.raise_for_status()
        summaries = response.json()

        properties_list = []
        for account in summaries.get('accountSummaries', []):
            for property in account.get('propertySummaries', []):
                properties_list.append({
                    'property_id': property['property'].split('/')[-1],
                    'display_name': property.get('displayName', 'Unnamed Property'),
                    'account_name': account.get('displayName', 'Unknown Account'),
                    'property_type': 'GA4'
                })

        return properties_list, None
    except Exception as e:
        error_message = f"Error fetching GA4 properties: {str(e)}"
        print(error_message)
        return [], error_message

def get_property_name(access_token, property_id):
    """Get the display name of a GA4 property"""
    try:
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }

        response = requests.get(
            f'https://analyticsadmin.googleapis.com/v1beta/properties/{property_id}',
            headers=headers
        )
        response.raise_for_status()
        data = response.json()
        return data.get('displayName', property_id)
    except:
        return property_id

def get_ga4_data(access_token, property_id, start_date, end_date):
    try:
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
            'X-TIMEZONE': 'UTC'
        }

        url = f'https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport'

        # Overall metrics request
        overall_request_body = {
            "dimensions": [
                {"name": "date"}
            ],
            "metrics": [
                {"name": "totalUsers"},
                {"name": "newUsers"},
                {"name": "activeUsers"},
                {"name": "eventCount"},
                {"name": "screenPageViews"},
                {"name": "userEngagementDuration"}
            ],
            "dateRanges": [{
                "startDate": start_date.strftime('%Y-%m-%d'),
                "endDate": end_date.strftime('%Y-%m-%d')
            }],
            "returnPropertyQuota": True
        }

        # Page metrics request
        page_request_body = {
            "dimensions": [
                {"name": "pagePath"}
            ],
            "metrics": [
                {"name": "screenPageViews"},
                {"name": "activeUsers"},
                {"name": "userEngagementDuration"}
            ],
            "dateRanges": [{
                "startDate": start_date.strftime('%Y-%m-%d'),
                "endDate": end_date.strftime('%Y-%m-%d')
            }],
            "limit": 10000
        }

        # Country demographics request
        country_request_body = {
            "dimensions": [
                {"name": "country"}
            ],
            "metrics": [
                {"name": "activeUsers"},
                {"name": "newUsers"},
                {"name": "engagedSessions"},
                {"name": "engagementRate"},
                {"name": "userEngagementDuration"}
            ],
            "dateRanges": [{
                "startDate": start_date.strftime('%Y-%m-%d'),
                "endDate": end_date.strftime('%Y-%m-%d')
            }],
            "orderBys": [
                {
                    "metric": {"metricName": "activeUsers"},
                    "desc": True
                }
            ],
            "limit": 250
        }

        # Event metrics request
        event_request_body = {
            "dimensions": [
                {"name": "eventName"}
            ],
            "metrics": [
                {"name": "eventCount"},
                {"name": "eventCountPerUser"}
            ],
            "dateRanges": [{
                "startDate": start_date.strftime('%Y-%m-%d'),
                "endDate": end_date.strftime('%Y-%m-%d')
            }],
            "orderBys": [{
                "metric": {"metricName": "eventCount"},
                "desc": True
            }],
            "limit": 10000
        }

        # Channel acquisition request
        channel_request_body = {
            "dimensions": [
                {"name": "firstUserSourceMedium"}
            ],
            "metrics": [
                {"name": "newUsers"},
                {"name": "sessionsPerUser"},
                {"name": "userEngagementDuration"}
            ],
            "dateRanges": [{
                "startDate": start_date.strftime('%Y-%m-%d'),
                "endDate": end_date.strftime('%Y-%m-%d')
            }],
            "orderBys": [{
                "metric": {"metricName": "newUsers"},
                "desc": True
            }],
            "limit": 2500
        }

        # Execute all requests
        overall_response = requests.post(url, headers=headers, json=overall_request_body)
        page_response = requests.post(url, headers=headers, json=page_request_body)
        country_response = requests.post(url, headers=headers, json=country_request_body)
        event_response = requests.post(url, headers=headers, json=event_request_body)
        channel_response = requests.post(url, headers=headers, json=channel_request_body)

        # Check responses
        for response in [overall_response, page_response, country_response, event_response, channel_response]:
            response.raise_for_status()

        # Parse responses
        overall_data = overall_response.json()
        page_data = page_response.json()
        country_data = country_response.json()
        event_data = event_response.json()
        channel_data = channel_response.json()

        # Process data
        processed_data = {
            'headers': ['Date', 'Page Views', 'Users', 'Avg. Session Duration'],
            'rows': [],
            'totals': {
                'totalActiveUsers': 0,
                'totalNewUsers': 0,
                'totalEventCount': 0,
                'totalViews': 0,
                'totalEngagementTime': 0
            },
            'pageMetrics': [],
            'countryMetrics': [],
            'eventMetrics': [],
            'channelMetrics': []
        }

        # Process overall metrics
        if 'rows' in overall_data:
            for row in overall_data['rows']:
                date_val = row['dimensionValues'][0]['value']
                metrics = row['metricValues']

                formatted_date = datetime.strptime(date_val, '%Y%m%d').strftime('%Y-%m-%d')
                page_views = int(metrics[4]['value'])
                users = int(metrics[2]['value'])  # activeUsers
                duration = float(metrics[5]['value'])  # userEngagementDuration

                processed_data['rows'].append([
                    formatted_date,
                    page_views,
                    users,
                    format_duration(duration)
                ])

                # Update totals
                processed_data['totals']['totalActiveUsers'] += users
                processed_data['totals']['totalNewUsers'] += int(metrics[1]['value'])
                processed_data['totals']['totalEventCount'] += int(metrics[3]['value'])
                processed_data['totals']['totalViews'] += page_views
                processed_data['totals']['totalEngagementTime'] += duration

        # Process page metrics
        if 'rows' in page_data:
            for row in page_data['rows']:
                processed_data['pageMetrics'].append({
                    'page': row['dimensionValues'][0]['value'],
                    'views': int(row['metricValues'][0]['value']),
                    'users': int(row['metricValues'][1]['value']),
                    'avgDuration': format_duration(float(row['metricValues'][2]['value']))
                })

        # Process country metrics
        if 'rows' in country_data:
            for row in country_data['rows']:
                processed_data['countryMetrics'].append({
                    'country': row['dimensionValues'][0]['value'],
                    'users': int(row['metricValues'][0]['value']),
                    'newUsers': int(row['metricValues'][1]['value']),
                    'engagedSessions': int(row['metricValues'][2]['value']),
                    'engagementRate': f"{float(row['metricValues'][3]['value']) * 100:.1f}%"
                })

        # Process event metrics
        if 'rows' in event_data:
            for row in event_data['rows']:
                processed_data['eventMetrics'].append({
                    'event': row['dimensionValues'][0]['value'],
                    'count': int(row['metricValues'][0]['value']),
                    'perUser': float(row['metricValues'][1]['value'])
                })

        # Process channel metrics
        if 'rows' in channel_data:
            total_new_users = sum(float(row['metricValues'][0]['value']) for row in channel_data['rows'])

            for row in channel_data['rows']:
                new_users = float(row['metricValues'][0]['value'])
                processed_data['channelMetrics'].append({
                    'source': row['dimensionValues'][0]['value'],
                    'newUsers': int(new_users),
                    'percentage': f"{(new_users / total_new_users * 100):.1f}%",
                    'sessionsPerUser': float(row['metricValues'][1]['value']),
                    'avgEngagementTime': format_duration(float(row['metricValues'][2]['value']))
                })

        return processed_data

    except Exception as e:
        print(f"Error fetching GA4 data: {e}")
        return None
