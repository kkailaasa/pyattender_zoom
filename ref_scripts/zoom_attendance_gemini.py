import os
import json
import requests
import datetime
import hmac
import hashlib
import pathlib
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from typing import Dict, List, Any, Optional, Set
import google.generativeai as genai
import asyncio

# Load environment variables
load_dotenv()

# App configuration
class Config:
    # NocoDB Configuration
    NOCODB_URL = os.getenv("NOCODB_URL", "https://km.koogle.sk")
    NOCODB_TOKEN = os.getenv("NOCODB_TOKEN")
    ROSTER_TABLE_ID = os.getenv("ROSTER_TABLE_ID", "m1848aw7em1uz9g")
    ATTENDANCE_TABLE_ID = os.getenv("ATTENDANCE_TABLE_ID", "mbur916jgs0m7ua")
    UNIDENTIFIED_TABLE_ID = os.getenv("UNIDENTIFIED_TABLE_ID", "mhsf4s0jhp90gnn")

    # API Key Authentication
    API_KEY = os.getenv("API_KEY")
    API_KEY_ENABLED = os.getenv("API_KEY_ENABLED", "true").lower() == "true"
    API_KEY_HEADER_NAME = os.getenv("API_KEY_HEADER_NAME", "x-api-key")

    # Zoom custom Headers
    ZOOM_CUSTOM_HEADER_KEY = os.getenv("ZOOM_CUSTOM_HEADER_KEY", "x-zoom-custom-auth")
    ZOOM_CUSTOM_HEADER_VALUE = os.getenv("ZOOM_CUSTOM_HEADER_VALUE")
    ZOOM_CUSTOM_HEADER_ENABLED = os.getenv("ZOOM_CUSTOM_HEADER_ENABLED", "false").lower() == "true"

    # Zoom webhook verification
    ZOOM_WEBHOOK_SECRET_TOKENS = []
    ZOOM_WEBHOOK_SECRET_VERIFIED = {}

    # AI Configuration
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
    USE_AI_MATCHING = os.getenv("USE_AI_MATCHING", "true").lower() == "true"
    CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.6"))

    # Debugging
    DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

    # Cache settings
    ROSTER_CACHE_SECONDS = int(os.getenv("ROSTER_CACHE_SECONDS", "600"))

    def __init__(self):
        self.load_zoom_tokens_from_env()
        self.validate_api_key_config()
        self.validate_zoom_custom_header_config()

    def validate_api_key_config(self):
        """Validate API key configuration"""
        if self.API_KEY_ENABLED and not self.API_KEY:
            print("WARNING: API key authentication is enabled but no API key is set. Set API_KEY in your .env file.")
            self.API_KEY_ENABLED = False

    def verify_zoom_custom_header(self, request_headers):
        """Verify Zoom custom header authentication"""
        if not self.ZOOM_CUSTOM_HEADER_ENABLED:
            return True  # If not enabled, skip this check

        custom_header_value = request_headers.get(self.ZOOM_CUSTOM_HEADER_KEY)
        if not custom_header_value:
            return False

        return custom_header_value == self.ZOOM_CUSTOM_HEADER_VALUE

    def validate_zoom_custom_header_config(self):
        """Validate Zoom custom header configuration"""
        if self.ZOOM_CUSTOM_HEADER_ENABLED and not self.ZOOM_CUSTOM_HEADER_VALUE:
            print("WARNING: Zoom custom header authentication is enabled but no value is set. Set ZOOM_CUSTOM_HEADER_VALUE in your .env file.")
            self.ZOOM_CUSTOM_HEADER_ENABLED = False

    def load_zoom_tokens_from_env(self):
        """Load webhook secret tokens and their verification status from environment variables"""
        # Clear existing tokens
        self.ZOOM_WEBHOOK_SECRET_TOKENS = []
        self.ZOOM_WEBHOOK_SECRET_VERIFIED = {}
        self.webhook_number_to_token = {}  # Add this mapping dictionary

        # Look for any webhook secrets with numeric identifiers
        for key, value in os.environ.items():
            if key.startswith('ZOOM_WEBHOOK_SECRET_'):
                try:
                    # Extract the numeric part of the key
                    if '_' in key:
                        parts = key.split('_')
                        if len(parts) >= 3 and parts[-1].isdigit():
                            webhook_number = parts[-1]

                            # Extract token and verification status
                            if '|' in value:
                                token, verified_str = value.split('|', 1)
                                verified = verified_str.lower() == 'true'
                            else:
                                token = value
                                verified = False

                            # Add to general token list
                            if token not in self.ZOOM_WEBHOOK_SECRET_TOKENS:
                                self.ZOOM_WEBHOOK_SECRET_TOKENS.append(token)

                            # Set verification status
                            self.ZOOM_WEBHOOK_SECRET_VERIFIED[token] = verified

                            # Map webhook number to token
                            self.webhook_number_to_token[webhook_number] = token

                            print(f"Loaded token for webhook number {webhook_number}, verified: {verified}")
                except Exception as e:
                    print(f"Error parsing token {key}: {e}")

        # Legacy token support
        legacy_token = os.getenv("ZOOM_WEBHOOK_SECRET")
        if legacy_token and legacy_token not in self.ZOOM_WEBHOOK_SECRET_TOKENS:
            self.ZOOM_WEBHOOK_SECRET_TOKENS.append(legacy_token)
            self.ZOOM_WEBHOOK_SECRET_VERIFIED[legacy_token] = True

        # Log what we found
        verified_count = sum(1 for v in self.ZOOM_WEBHOOK_SECRET_VERIFIED.values() if v)
        print(f"Loaded {len(self.ZOOM_WEBHOOK_SECRET_TOKENS)} Zoom token(s), {verified_count} already verified")
        print(f"Mapped webhook numbers: {list(self.webhook_number_to_token.keys())}")

    def get_token_by_endpoint_number(self, endpoint_number):
        """
        Get the corresponding token for the specified webhook number.
        Returns the token and whether it's verified.
        """
        # Convert to string to ensure consistency
        endpoint_number = str(endpoint_number)

        # Check if we have a direct mapping for this webhook number
        if hasattr(self, 'webhook_number_to_token') and endpoint_number in self.webhook_number_to_token:
            token = self.webhook_number_to_token[endpoint_number]
            is_verified = self.ZOOM_WEBHOOK_SECRET_VERIFIED.get(token, False)
            return token, is_verified

        # Fallback to the old index-based method
        try:
            # Convert to integer and adjust for zero-based indexing
            index = int(endpoint_number) - 1

            # Check if the index is valid
            if 0 <= index < len(self.ZOOM_WEBHOOK_SECRET_TOKENS):
                token = self.ZOOM_WEBHOOK_SECRET_TOKENS[index]
                is_verified = self.ZOOM_WEBHOOK_SECRET_VERIFIED.get(token, False)
                return token, is_verified
        except ValueError:
            pass

        print(f"WARNING: No token found for webhook number {endpoint_number}")
        return None, False

    def verify_zoom_signature_for_endpoint(self, signature, timestamp, request_body, endpoint_number):
        """
        Verify Zoom webhook signature using the token associated with the specific endpoint number.
        """
        if not signature.startswith("v0="):
            return False

        received_hash = signature[3:]  # Remove 'v0='

        # Get the token for this specific endpoint
        token, _ = self.get_token_by_endpoint_number(endpoint_number)
        if not token:
            # If no specific token found, fall back to default behavior
            return self.verify_zoom_signature(signature, timestamp, request_body)

        # For compatibility with different message formats
        message = f"v0:{timestamp}:{request_body.decode('utf-8')}"

        # Generate expected hash with this token
        expected_hash = hmac.new(
            token.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        # Compare signatures
        return hmac.compare_digest(received_hash, expected_hash)

    def save_verification_status(self):
        """Save verification status to .env file"""
        try:
            # Read existing .env file
            env_path = os.path.join(os.getcwd(), '.env')
            if not os.path.exists(env_path):
                print("Warning: .env file not found, cannot save verification status")
                return

            with open(env_path, 'r') as file:
                lines = file.readlines()

            # Update token verification status in .env content
            new_lines = []
            token_entries_updated = set()

            for line in lines:
                line = line.strip()
                if line.startswith('ZOOM_WEBHOOK_SECRET_'):
                    # Extract the token number and current value
                    parts = line.split('=', 1)
                    if len(parts) != 2:
                        new_lines.append(line)
                        continue

                    key, value = parts
                    token_num = key.split('_')[-1]

                    # Find if this is a token we know about
                    try:
                        token_index = int(token_num) - 1
                        if token_index < len(self.ZOOM_WEBHOOK_SECRET_TOKENS):
                            token = self.ZOOM_WEBHOOK_SECRET_TOKENS[token_index]
                            token_entries_updated.add(token)
                            # Replace or add verification status
                            if '|' in value:
                                token_value = value.split('|', 1)[0]
                            else:
                                token_value = value
                            new_line = f"{key}={token_value}|{str(self.ZOOM_WEBHOOK_SECRET_VERIFIED[token]).lower()}"
                            new_lines.append(new_line)
                        else:
                            new_lines.append(line)
                    except (ValueError, IndexError):
                        new_lines.append(line)
                elif line.startswith('ZOOM_WEBHOOK_SECRET='):
                    # Handle legacy token format
                    parts = line.split('=', 1)
                    if len(parts) == 2:
                        legacy_token = parts[1]
                        if legacy_token in self.ZOOM_WEBHOOK_SECRET_TOKENS:
                            token_entries_updated.add(legacy_token)
                            new_lines.append(line)  # Keep the original line
                else:
                    new_lines.append(line)

            # Add any tokens that weren't already in the file
            for i, token in enumerate(self.ZOOM_WEBHOOK_SECRET_TOKENS):
                if token not in token_entries_updated:
                    # This is a new token that wasn't in the file
                    new_lines.append(f"ZOOM_WEBHOOK_SECRET_{i+1}={token}|{str(self.ZOOM_WEBHOOK_SECRET_VERIFIED[token]).lower()}")

            # Write updated content back to .env file
            with open(env_path, 'w') as file:
                file.write('\n'.join(new_lines))

            print(f"Saved verification status to .env file")

        except Exception as e:
            print(f"Error saving verification status: {e}")

    def get_next_unverified_token(self) -> str:
        """Get the next unverified token in sequence"""
        for token in self.ZOOM_WEBHOOK_SECRET_TOKENS:
            if not self.ZOOM_WEBHOOK_SECRET_VERIFIED[token]:
                return token
        # If all verified, return first token
        return self.ZOOM_WEBHOOK_SECRET_TOKENS[0] if self.ZOOM_WEBHOOK_SECRET_TOKENS else ""

    def mark_token_as_verified(self, token: str):
        """Mark a specific token as verified and save status"""
        if token in self.ZOOM_WEBHOOK_SECRET_VERIFIED:
            self.ZOOM_WEBHOOK_SECRET_VERIFIED[token] = True
            # Save the updated verification status to .env
            self.save_verification_status()

    def verify_zoom_signature(self, signature: str, timestamp: str, request_body: bytes) -> bool:
        """Verify Zoom webhook signature using all registered tokens."""
        if not signature.startswith("v0="):
            return False

        received_hash = signature[3:]  # Remove 'v0='

        # For compatibility with different message formats
        message = f"v0:{timestamp}:{request_body.decode('utf-8')}"

        for token in self.ZOOM_WEBHOOK_SECRET_TOKENS:
            # Generate expected hash with this token
            expected_hash = hmac.new(
                token.encode('utf-8'),
                message.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()

            # Compare signatures
            if hmac.compare_digest(received_hash, expected_hash):
                return True

        # If we get here, no token worked
        return False

    def __str__(self):
        """Return a string representation of the config (excluding sensitive values)"""
        return {
            "NOCODB_URL": self.NOCODB_URL,
            "ROSTER_TABLE_ID": self.ROSTER_TABLE_ID,
            "ATTENDANCE_TABLE_ID": self.ATTENDANCE_TABLE_ID,
            "UNIDENTIFIED_TABLE_ID": self.UNIDENTIFIED_TABLE_ID,
            "USE_AI_MATCHING": self.USE_AI_MATCHING,
            "CONFIDENCE_THRESHOLD": self.CONFIDENCE_THRESHOLD,
            "DEBUG_MODE": self.DEBUG_MODE,
            "ROSTER_CACHE_SECONDS": self.ROSTER_CACHE_SECONDS,
            "ZOOM_TOKENS_COUNT": len(self.ZOOM_WEBHOOK_SECRET_TOKENS),
            "ZOOM_TOKENS_VERIFIED": sum(1 for v in self.ZOOM_WEBHOOK_SECRET_VERIFIED.values() if v)
        }.__str__()

# Create config instance
config = Config()

# Initialize FastAPI app
app = FastAPI()

# API Key Middleware
class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Skip API key check for Zoom webhook endpoint
        if request.url.path == "/zoom/webhook":
            return await call_next(request)

        # Skip API key check if API key authentication is disabled
        if not config.API_KEY_ENABLED:
            return await call_next(request)

        # Get API key from request header
        api_key = request.headers.get(config.API_KEY_HEADER_NAME)

        # Validate API key
        if api_key != config.API_KEY:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid API key or missing API key header"}
            )

        # Continue with the request
        return await call_next(request)

# Add API key middleware to app
app.add_middleware(APIKeyMiddleware)

# Configure Gemini if API key is available
if config.GOOGLE_API_KEY:
    genai.configure(api_key=config.GOOGLE_API_KEY)
else:
    print("WARNING: No Google API key provided. AI matching will be disabled.")
    config.USE_AI_MATCHING = False

class AttendanceProcessor:
    def __init__(self):
        self.roster_cache = []
        self.roster_last_updated = None
        self.cache_lifetime = config.ROSTER_CACHE_SECONDS

        # Initialize Gemini model if AI matching is enabled
        if config.USE_AI_MATCHING:
            try:
                self.model = genai.GenerativeModel(
                    model_name="gemini-2.0-flash-001",
                    system_instruction="""
                    You are an attendance matching assistant. Your job is to match participant names from
                    Zoom meetings with their official names in a roster database.

                    - Match the names based on spelling ONLY, as closely as possible.

                    - When considering spiritual names, "Sri Nithya" is prefix and "Ananda" can also be prefix or suffix. The name in the middle is there actual name. For ex; if the name is "Sri Nithya Jnanapradhatha" "Sri Nithya" is prefix, Jnanapradhatha is the actual name, so matching should be done based on the name in the middle.

                    - Return the best match with confidence score.
                    """
                )
                print("Successfully initialized Gemini AI model")
            except Exception as e:
                print(f"Failed to initialize Gemini AI model: {str(e)}")
                config.USE_AI_MATCHING = False

    async def get_roster(self, force_refresh=False):
        """Fetch the roster from NocoDB, with caching."""
        current_time = datetime.datetime.now()

        # Check if cache is valid
        if (not force_refresh and
            self.roster_last_updated and
            (current_time - self.roster_last_updated).seconds < self.cache_lifetime and
            self.roster_cache):
            return self.roster_cache

        # Fetch from API if cache is invalid
        headers = {"xc-token": config.NOCODB_TOKEN}
        all_roster = []
        page = 1
        limit = 100  # Adjust based on your data size

        # Handle pagination
        while True:
            response = requests.get(
                f"{config.NOCODB_URL}/api/v2/tables/{config.ROSTER_TABLE_ID}/records",
                params={"limit": limit, "offset": (page - 1) * limit},
                headers=headers
            )

            if response.status_code != 200:
                raise HTTPException(status_code=500, detail=f"Failed to get roster: {response.text}")

            data = response.json()
            all_roster.extend(data.get("list", []))

            # Check if we need to fetch more pages
            page_info = data.get("PageInfo", {})
            if page_info.get("isLastPage", True):
                break

            page += 1

        # Update cache
        self.roster_cache = all_roster
        self.roster_last_updated = current_time

        return all_roster

    async def mark_attendance(self, person_id, attendance_date):
        """Mark attendance for a person in the attendance table."""
        headers = {
            "xc-token": config.NOCODB_TOKEN,
            "Content-Type": "application/json"
        }

        # Use the date directly as the column name (already in YYYY-MM-DD format)
        date_column = attendance_date  # No need to replace - with _

        payload = {
            "Id": str(person_id),
            f"{date_column}": "Yes"
        }

        response = requests.patch(
            f"{config.NOCODB_URL}/api/v2/tables/{config.ATTENDANCE_TABLE_ID}/records",
            json=payload,
            headers=headers
        )

        if response.status_code not in [200, 201]:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to mark attendance: {response.text}"
            )

        return response.json()

    async def log_unidentified_participant(self, name, join_time, date):
        """Log unidentified participants to the unidentified table."""
        headers = {
            "xc-token": config.NOCODB_TOKEN,
            "Content-Type": "application/json"
        }

        # Format time for better readability
        join_time_formatted = datetime.datetime.fromisoformat(join_time.replace('Z', '+00:00')).strftime("%H:%M")

        payload = {
            "Date": date,
            "joinedTime": join_time_formatted,
            "nameJoinedWith": name
        }

        response = requests.post(
            f"{config.NOCODB_URL}/api/v2/tables/{config.UNIDENTIFIED_TABLE_ID}/records",
            json=payload,
            headers=headers
        )

        if response.status_code not in [200, 201]:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to log unidentified participant: {response.text}"
            )

        return response.json()

    async def match_participant_with_roster(self, participant_name, roster):
        """
        Use Gemini AI to match participant names with the roster.
        Returns the matched person ID and confidence score.

        This simplified version doesn't use function calling but instead
        relies on structured JSON output in the response.
        """
        # Extract relevant roster information for matching
        roster_info = []
        for person in roster:
            person_info = {
                "Id": person.get("Id"),
                "firstName": person.get("firstName", ""),
                "lastName": person.get("lastName", ""),
                "spiritualName": person.get("spiritualName"),
                "fullName": f"{person.get('firstName', '')} {person.get('lastName', '')}".strip()
            }
            roster_info.append(person_info)

        # Create the prompt for Gemini
        prompt = f"""
        I need to match a Zoom participant with their entry in our roster database.

        Zoom participant name: "{participant_name}"

        Roster database (showing ID, firstName, lastName, and spiritualName if available):
        {json.dumps(roster_info, indent=2)}

        Find the best match for the participant, considering:

        1. The participant name might have typos

        2. Match the names based on spelling ONLY, as closely as possible.

        3. When considering spiritual names, "Sri Nithya" is prefix and "Ananda" can also be prefix or suffix. The name in the middle is there actual name. For ex; if the name is "Sri Nithya Jnanapradhatha" "Sri Nithya" is prefix, Jnanapradhatha is the actual name, so matching should be done based on the name in the middle.

        If the confidence is below 0.6, report no match found.

        Provide your answer in this JSON format (and only this format, no explanations outside the JSON):
        {{
            "matchedPersonId": ID_number_or_null,
            "confidence": confidence_score_between_0_and_1,
            "reasoning": "brief explanation of your matching decision"
        }}

        Example:
        {{
            "matchedPersonId": 42,
            "confidence": 0.85,
            "reasoning": "The participant name 'John S.' is likely a shortened version of 'John Smith' (ID 42)"
        }}

        Or for no match:
        {{
            "matchedPersonId": null,
            "confidence": 0.3,
            "reasoning": "No convincing match found for 'XYZ User'"
        }}
        """

        try:
            response = self.model.generate_content(
                prompt,
                generation_config={
                    "temperature": 0.2,
                    "top_p": 0.95,
                    "top_k": 40,
                    "max_output_tokens": 2048,
                    "response_mime_type": "application/json",
                },
            )

            # Get the text response
            text_response = response.text

            # Try to parse JSON from the response
            try:
                # Strip any potential markdown code block syntax
                cleaned_text = text_response.replace("```json", "").replace("```", "").strip()
                match_data = json.loads(cleaned_text)

                # Validate the expected fields
                if isinstance(match_data, dict) and "matchedPersonId" in match_data:
                    return match_data
                else:
                    raise ValueError("Invalid response format")

            except json.JSONDecodeError as e:
                print(f"Failed to parse JSON from response: {e}")
                print(f"Response text: {text_response}")

                # Try to extract JSON with regex as fallback
                import re
                json_match = re.search(r'\{.*\}', text_response, re.DOTALL)
                if json_match:
                    try:
                        match_data = json.loads(json_match.group(0))
                        if isinstance(match_data, dict) and "matchedPersonId" in match_data:
                            return match_data
                    except json.JSONDecodeError:
                        pass

                # Return a default response if parsing fails
                return {
                    "matchedPersonId": None,
                    "confidence": 0,
                    "reasoning": f"Failed to parse response: {str(e)}"
                }

        except Exception as e:
            print(f"Error in AI matching: {str(e)}")
            return {
                "matchedPersonId": None,
                "confidence": 0,
                "reasoning": f"Error in AI processing: {str(e)}"
            }

    # Fix for the simple_name_matching method

    def simple_name_matching(self, participant_name, roster):
        """
        Match a participant name from Zoom to a person in the roster using simple string matching.

        Args:
            participant_name (str): Name of the participant from Zoom
            roster (list): List of people from the roster

        Returns:
            dict: The matched person from the roster, or None if no match found
        """
        if not participant_name or not roster:
            return None

        # Clean and normalize the participant name
        participant_name = participant_name.lower().strip()

        # Try to find exact matches first
        for person in roster:
            # Skip if person is None
            if person is None:
                continue

            # Get first and last names, with fallback to empty string for None values
            first_name = (person.get("firstName") or "").lower()
            last_name = (person.get("lastName") or "").lower()
            spiritual_name = (person.get("spiritualName") or "").lower()

            # Check for exact matches
            full_name = f"{first_name} {last_name}".strip()
            if participant_name == full_name:
                return person

            # Check reversed name (last name first)
            reversed_name = f"{last_name} {first_name}".strip()
            if participant_name == reversed_name:
                return person

            # Check spiritual name matches if available
            if spiritual_name and participant_name == spiritual_name:
                return person

        # If no exact match, try partial matches
        best_match = None
        best_score = 0

        for person in roster:
            # Skip if person is None
            if person is None:
                continue

            # Get first and last names, with fallback to empty string for None values
            first_name = (person.get("firstName") or "").lower()
            last_name = (person.get("lastName") or "").lower()
            spiritual_name = (person.get("spiritualName") or "").lower()

            # Calculate match scores
            score = 0

            # Check if first name is in participant name
            if first_name and first_name in participant_name:
                score += len(first_name)

            # Check if last name is in participant name
            if last_name and last_name in participant_name:
                score += len(last_name)

            # Check if spiritual name is in participant name
            if spiritual_name and spiritual_name in participant_name:
                score += len(spiritual_name)

            # Update best match if this score is higher
            if score > best_score:
                best_score = score
                best_match = person

        # Return the best match if it meets a minimum threshold (adjust as needed)
        return best_match if best_score > 2 else None

    async def process_participant_joined(self, webhook_data):
        """Process participant joined event and handle attendance marking."""
        try:
            if "payload" not in webhook_data or "object" not in webhook_data["payload"]:
                return {"status": "error", "message": "Invalid webhook data format"}

            obj = webhook_data["payload"]["object"]
            participant = obj.get("participant", {})

            participant_name = participant.get("user_name", "Unknown")
            join_time = participant.get("join_time")

            # Get today's date in YYYY-MM-DD format from the join_time
            if join_time:
                today_date = join_time.split("T")[0]  # Extract YYYY-MM-DD from ISO format
            else:
                # Fallback to current date if join_time is not available
                today_date = datetime.datetime.now().strftime("%Y-%m-%d")

            # Get roster list
            roster = await self.get_roster()

            if not roster:
                print(f"Warning: Empty roster data, cannot match participant: {participant_name}")

                # Still record the unmatched participant
                unidentified_result = await self.log_unidentified_participant(
                    participant_name, join_time, today_date
                )
                return {
                    "status": "success",
                    "action": "logged_unidentified",
                    "name": participant_name,
                    "confidence": 0,
                    "reasoning": "Empty roster"
                }

            # Initialize match variables
            match_result = None
            person_id = None
            confidence = 0
            reasoning = ""

            # Try Gemini AI first if enabled
            if config.USE_AI_MATCHING:
                try:
                    match_result = await self.match_participant_with_roster(participant_name, roster)

                    # Check if we got a valid result from Gemini
                    if match_result and "matchedPersonId" in match_result:
                        person_id = match_result.get("matchedPersonId")
                        confidence = match_result.get("confidence", 0)
                        reasoning = match_result.get("reasoning", "")

                        # Log the AI matching attempt
                        print(f"AI Matching '{participant_name}': ID={person_id}, Confidence={confidence}")
                    else:
                        # If Gemini didn't return a valid structure, use fallback
                        raise ValueError("Invalid AI matching result structure")
                except Exception as e:
                    # Log the error and fall back to simple matching
                    print(f"AI matching failed, using fallback: {str(e)}")
                    match_result = self.simple_name_matching(participant_name, roster)

                    # Check if simple matching found a match
                    if match_result:
                        person_id = match_result.get("matchedPersonId") or match_result.get("Id")
                        confidence = match_result.get("confidence", 0)
                        reasoning = match_result.get("reasoning", "") + " (via fallback matching)"

                        # Log the fallback matching attempt
                        print(f"Fallback matching '{participant_name}': ID={person_id}, Confidence={confidence}, Reason={reasoning}")
                    else:
                        print(f"No match found for participant: {participant_name}")
            else:
                # AI matching is disabled, use simple matching directly
                match_result = self.simple_name_matching(participant_name, roster)

                # Check if simple matching found a match
                if match_result:
                    person_id = match_result.get("matchedPersonId") or match_result.get("Id")
                    confidence = match_result.get("confidence", 0)
                    reasoning = match_result.get("reasoning", "")

                    # Log the matching attempt
                    print(f"Simple matching '{participant_name}': ID={person_id}, Confidence={confidence}, Reason={reasoning}")
                else:
                    print(f"No match found for participant: {participant_name}")

            if person_id and confidence >= config.CONFIDENCE_THRESHOLD:
                # Found a match with good confidence - mark attendance
                try:
                    attendance_result = await self.mark_attendance(person_id, today_date)
                    return {
                        "status": "success",
                        "action": "marked_attendance",
                        "personId": person_id,
                        "confidence": confidence,
                        "reasoning": reasoning
                    }
                except Exception as e:
                    return {
                        "status": "error",
                        "message": f"Failed to mark attendance: {str(e)}",
                        "personId": person_id,
                        "confidence": confidence
                    }
            else:
                # No good match found - log as unidentified
                try:
                    unidentified_result = await self.log_unidentified_participant(
                        participant_name, join_time, today_date
                    )
                    return {
                        "status": "success",
                        "action": "logged_unidentified",
                        "name": participant_name,
                        "confidence": confidence,
                        "reasoning": reasoning or "No match found or confidence too low"
                    }
                except Exception as e:
                    return {
                        "status": "error",
                        "message": f"Failed to log unidentified participant: {str(e)}"
                    }
        except Exception as e:
            # Catch all other exceptions
            import traceback
            print(f"Error processing participant joined: {str(e)}")
            print(traceback.format_exc())
            return {"status": "error", "message": f"Internal error: {str(e)}"}

    def store_raw_webhook(self, meeting_uuid: str, data: Dict[str, Any]) -> None:
        """Store raw webhook data to file system"""
        # Get current date for folder structure
        now = datetime.datetime.now()
        date_str = now.strftime('%Y-%m-%d')
        timestamp = now.strftime('%Y-%m-%d_%H-%M-%S-%f')

        # Create directory structure
        raw_dir = pathlib.Path("Raw")
        day_dir = raw_dir / date_str
        meeting_dir = day_dir / meeting_uuid
        meeting_dir.mkdir(parents=True, exist_ok=True)

        # Write webhook data to file
        file_path = meeting_dir / f"{timestamp}.json"
        with open(file_path, 'w') as f:
            json.dump(data, f, indent=2)

# Initialize the processor
attendance_processor = AttendanceProcessor()

# Zoom webhook endpoint with custom header verification
@app.post("/zoom/webhook")
async def zoom_webhook(request: Request):
    """Process Zoom webhook events with custom header verification."""
    # Get the raw body for signature verification
    body = await request.body()
    body_str = body.decode("utf-8")

    # Log the raw request details
    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{current_time}] Raw webhook received: {body_str[:200]}...")
    print(f"[{current_time}] Headers: {dict(request.headers)}")

    # Custom header verification
    if config.ZOOM_CUSTOM_HEADER_ENABLED:
        custom_header_verified = config.verify_zoom_custom_header(request.headers)
        if not custom_header_verified:
            print(f"[{current_time}] Custom header verification failed")
            raise HTTPException(status_code=401, detail="Invalid custom header authentication")
        else:
            print(f"[{current_time}] Custom header verification successful")

    # Try to parse JSON
    try:
        data = json.loads(body_str)
    except json.JSONDecodeError as e:
        print(f"[{current_time}] JSON parse error: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")

    event_type = data.get("event")
    print(f"[{current_time}] Webhook received: {event_type}")

    # Extract meeting UUID if available for raw data storage
    meeting_uuid = None
    if "payload" in data and "object" in data["payload"]:
        obj = data["payload"]["object"]
        if "uuid" in obj:
            meeting_uuid = obj["uuid"]
            # Store raw webhook data (you would need your own implementation of this function)
            # attendance_processor.store_raw_webhook(meeting_uuid, data)

    # Case 1: Handle Zoom endpoint verification
    if event_type == "endpoint.url_validation":
        print(f"[{current_time}] Processing endpoint validation")
        print(f"[{current_time}] Full validation payload: {json.dumps(data)}")

        plain_token = data.get("payload", {}).get("plainToken")
        if not plain_token:
            print(f"[{current_time}] Error: No plain token provided")
            raise HTTPException(status_code=400, detail="No plain token provided")

        # Use the next unverified token for validation
        current_token = config.get_next_unverified_token()
        if not current_token:
            print(f"[{current_time}] Error: No webhook tokens configured")
            raise HTTPException(status_code=500, detail="No webhook tokens configured")

        print(f"[{current_time}] Using token (first 5 chars): {current_token[:5]}...")
        print(f"[{current_time}] Plain token from Zoom: {plain_token}")

        # Generate hash using the helper function
        def generate_hash(message: str, secret: str) -> str:
            return hmac.new(
                secret.encode('utf-8'),
                message.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()

        encrypted_token = generate_hash(plain_token, current_token)

        print(f"[{current_time}] Generated encrypted token: {encrypted_token}")

        # Mark this token as verified
        config.mark_token_as_verified(current_token)

        # Log verification status
        verified_count = sum(1 for v in config.ZOOM_WEBHOOK_SECRET_VERIFIED.values() if v)
        total_count = len(config.ZOOM_WEBHOOK_SECRET_TOKENS)
        print(f"[{current_time}] Verified {verified_count} out of {total_count} accounts")

        # Prepare response
        response = {
            "plainToken": plain_token,
            "encryptedToken": encrypted_token
        }

        print(f"[{current_time}] Validation response: {json.dumps(response)}")
        return JSONResponse(content=response)

    # Case 2: Verify signature for regular webhook events
    signature = request.headers.get("x-zm-signature", "")
    timestamp = request.headers.get("x-zm-request-timestamp", "")

    print(f"[{current_time}] Webhook headers - Signature: {signature}, Timestamp: {timestamp}")

    if signature and timestamp:
        signature_valid = config.verify_zoom_signature(signature, timestamp, body)
        print(f"[{current_time}] Signature verification result: {signature_valid}")

        if not signature_valid:
            print(f"[{current_time}] Invalid signature - rejecting webhook")
            raise HTTPException(status_code=401, detail="Invalid signature")
    elif config.ZOOM_WEBHOOK_SECRET_TOKENS:
        # Only enforce signature check if tokens are configured
        print(f"[{current_time}] Missing signature or timestamp headers")
        raise HTTPException(status_code=401, detail="Missing signature headers")

    # Process based on event type
    if event_type == "meeting.participant_joined":
        print(f"[{current_time}] Processing participant joined event")
        result = await attendance_processor.process_participant_joined(data)
        print(f"[{current_time}] Participant processing result: {json.dumps(result)}")
        return result
        return {"status": "success", "message": "Participant joined event received"}
    else:
        # For other event types, just acknowledge receipt
        print(f"[{current_time}] Event {event_type} received but not processed")
        return {"status": "success", "message": f"Event {event_type} received but not processed"}

@app.post("/zoom/webhook_{endpoint_number}")
async def zoom_webhook_underscore(endpoint_number: str, request: Request):
    """Process Zoom webhook events with underscore format."""
    # Get the raw body for signature verification
    body = await request.body()
    body_str = body.decode("utf-8")

    # Log the raw request details
    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{current_time}] Raw webhook received at endpoint_{endpoint_number}: {body_str[:200]}...")
    print(f"[{current_time}] Headers: {dict(request.headers)}")

    # Get the token for this webhook number
    token, is_verified = config.get_token_by_endpoint_number(endpoint_number)
    print(f"[{current_time}] Token lookup for webhook_{endpoint_number}: found={token is not None}, verified={is_verified}")

    # Custom header verification
    if config.ZOOM_CUSTOM_HEADER_ENABLED:
        custom_header_verified = config.verify_zoom_custom_header(request.headers)
        if not custom_header_verified:
            print(f"[{current_time}] Custom header verification failed")
            raise HTTPException(status_code=401, detail="Invalid custom header authentication")
        else:
            print(f"[{current_time}] Custom header verification successful")

    # Try to parse JSON
    try:
        data = json.loads(body_str)
    except json.JSONDecodeError as e:
        print(f"[{current_time}] JSON parse error: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")

    event_type = data.get("event")
    print(f"[{current_time}] Webhook received: {event_type}")

    # Extract meeting UUID if available for raw data storage
    meeting_uuid = None
    if "payload" in data and "object" in data["payload"]:
        obj = data["payload"]["object"]
        if "uuid" in obj:
            meeting_uuid = obj["uuid"]

    # Case 1: Handle Zoom endpoint verification
    if event_type == "endpoint.url_validation":
        print(f"[{current_time}] Processing endpoint validation for endpoint {endpoint_number}")
        print(f"[{current_time}] Full validation payload: {json.dumps(data)}")

        plain_token = data.get("payload", {}).get("plainToken")
        if not plain_token:
            print(f"[{current_time}] Error: No plain token provided")
            raise HTTPException(status_code=400, detail="No plain token provided")

        # Get the token corresponding to this endpoint number
        token, is_verified = config.get_token_by_endpoint_number(endpoint_number)
        if not token:
            print(f"[{current_time}] Error: No token configured for endpoint {endpoint_number}")
            raise HTTPException(status_code=500, detail=f"No token configured for endpoint {endpoint_number}")

        print(f"[{current_time}] Using token (first 5 chars): {token[:5]}...")
        print(f"[{current_time}] Plain token from Zoom: {plain_token}")

        # Generate hash
        def generate_hash(message: str, secret: str) -> str:
            return hmac.new(
                secret.encode('utf-8'),
                message.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()

        encrypted_token = generate_hash(plain_token, token)
        print(f"[{current_time}] Generated encrypted token: {encrypted_token}")

        # Mark this token as verified
        config.mark_token_as_verified(token)

        # Log verification status
        verified_count = sum(1 for v in config.ZOOM_WEBHOOK_SECRET_VERIFIED.values() if v)
        total_count = len(config.ZOOM_WEBHOOK_SECRET_TOKENS)
        print(f"[{current_time}] Verified {verified_count} out of {total_count} accounts")

        # Prepare response
        response = {
            "plainToken": plain_token,
            "encryptedToken": encrypted_token
        }

        print(f"[{current_time}] Validation response: {json.dumps(response)}")
        return JSONResponse(content=response)

    # Case 2: Verify signature for regular webhook events
    signature = request.headers.get("x-zm-signature", "")
    timestamp = request.headers.get("x-zm-request-timestamp", "")

    print(f"[{current_time}] Webhook headers - Signature: {signature}, Timestamp: {timestamp}")

    if signature and timestamp:
        # Use endpoint-specific verification
        signature_valid = config.verify_zoom_signature_for_endpoint(
            signature, timestamp, body, endpoint_number
        )
        print(f"[{current_time}] Signature verification result for endpoint {endpoint_number}: {signature_valid}")

        if not signature_valid:
            print(f"[{current_time}] Invalid signature - rejecting webhook")
            raise HTTPException(status_code=401, detail="Invalid signature")
    elif config.ZOOM_WEBHOOK_SECRET_TOKENS:
        # Only enforce signature check if tokens are configured
        print(f"[{current_time}] Missing signature or timestamp headers")
        raise HTTPException(status_code=401, detail="Missing signature headers")

    # Process based on event type
    if event_type == "meeting.participant_joined":
        print(f"[{current_time}] Processing participant joined event")
        result = await attendance_processor.process_participant_joined(data)
        print(f"[{current_time}] Participant processing result: {json.dumps(result)}")
        return result
        return {"status": "success", "message": "Participant joined event received"}
    else:
        # For other event types, just acknowledge receipt
        print(f"[{current_time}] Event {event_type} received but not processed")
        return {"status": "success", "message": f"Event {event_type} received but not processed"}


@app.get("/test")
async def test_endpoint():
    """Simple health check endpoint."""
    return {"status": "ok"}

@app.get("/verification-status")
async def get_verification_status():
    """Endpoint to check verification status"""
    verified_count = sum(1 for v in config.ZOOM_WEBHOOK_SECRET_VERIFIED.values() if v)
    total_count = len(config.ZOOM_WEBHOOK_SECRET_TOKENS)
    return {
        "total_accounts": total_count,
        "verified_accounts": verified_count,
        "status_by_token": {
            f"account_{i+1}": verified
            for i, verified in enumerate(config.ZOOM_WEBHOOK_SECRET_VERIFIED.values())
        },
        "custom_header_enabled": config.ZOOM_CUSTOM_HEADER_ENABLED,
        "custom_header_key": config.ZOOM_CUSTOM_HEADER_KEY
    }

@app.post("/reset-token")
async def reset_token(request: Request):
    """Reset verification status for all tokens or a specific token"""
    try:
        # Check if body is provided to reset specific token
        body = await request.body()
        if body:
            try:
                data = json.loads(body)
                token = data.get("token")

                if token:
                    # Check if token exists in our list
                    if token not in config.ZOOM_WEBHOOK_SECRET_TOKENS:
                        raise HTTPException(status_code=404, detail="Token not found")

                    # Reset the verification status for specific token
                    config.ZOOM_WEBHOOK_SECRET_VERIFIED[token] = False

                    # Find the account number for this token
                    account_number = config.ZOOM_WEBHOOK_SECRET_TOKENS.index(token) + 1

                    # Save the updated verification status
                    config.save_verification_status()

                    return {
                        "status": "success",
                        "message": f"Verification status reset for account {account_number}",
                        "account_number": account_number
                    }
            except json.JSONDecodeError:
                # If JSON is invalid, continue to reset all tokens
                pass

        # Reset all tokens
        for token in config.ZOOM_WEBHOOK_SECRET_TOKENS:
            config.ZOOM_WEBHOOK_SECRET_VERIFIED[token] = False

        # Save the updated verification status
        config.save_verification_status()

        return {
            "status": "success",
            "message": f"Verification status reset for all {len(config.ZOOM_WEBHOOK_SECRET_TOKENS)} accounts",
            "total_accounts": len(config.ZOOM_WEBHOOK_SECRET_TOKENS)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error resetting tokens: {str(e)}")

@app.get("/debug")
async def debug_info():
    """Get debug information about the current setup."""
    if not config.DEBUG_MODE:
        return {"status": "Debug mode disabled. Enable by setting DEBUG_MODE=true in .env"}

    try:
        # Get roster size
        roster_count = 0
        roster_error = None
        try:
            roster = await attendance_processor.get_roster(force_refresh=True)
            roster_count = len(roster)
        except Exception as e:
            roster_error = str(e)

        # Test NocoDB connection
        nocodb_status = "Unknown"
        try:
            headers = {"xc-token": config.NOCODB_TOKEN}
            response = requests.get(
                f"{config.NOCODB_URL}/api/v2/tables/{config.ROSTER_TABLE_ID}/records",
                params={"limit": 1},
                headers=headers
            )
            nocodb_status = f"OK - Status {response.status_code}"
        except Exception as e:
            nocodb_status = f"Error: {str(e)}"

        # Test AI model if enabled
        ai_status = "Disabled"
        if config.USE_AI_MATCHING:
            try:
                # Simple test of the AI model
                response = attendance_processor.model.generate_content("Hello, are you working?")
                ai_status = f"OK - Response: {response.text[:50]}..."
            except Exception as e:
                ai_status = f"Error: {str(e)}"

        # Check zoom tokens
        zoom_tokens_status = {
            "total_tokens": len(config.ZOOM_WEBHOOK_SECRET_TOKENS),
            "verified_tokens": sum(1 for v in config.ZOOM_WEBHOOK_SECRET_VERIFIED.values() if v),
            "token_verification_status": {
                f"token_{i+1}": {"verified": verified}
                for i, verified in enumerate(config.ZOOM_WEBHOOK_SECRET_VERIFIED.values())
            }
        }

        return {
            "status": "ok",
            "config": {
                "NOCODB_URL": config.NOCODB_URL,
                "ROSTER_TABLE_ID": config.ROSTER_TABLE_ID,
                "ATTENDANCE_TABLE_ID": config.ATTENDANCE_TABLE_ID,
                "UNIDENTIFIED_TABLE_ID": config.UNIDENTIFIED_TABLE_ID,
                "USE_AI_MATCHING": config.USE_AI_MATCHING,
                "CONFIDENCE_THRESHOLD": config.CONFIDENCE_THRESHOLD,
                "DEBUG_MODE": config.DEBUG_MODE,
                "ROSTER_CACHE_SECONDS": config.ROSTER_CACHE_SECONDS
            },
            "status_checks": {
                "nocodb_connection": nocodb_status,
                "roster_count": roster_count,
                "roster_error": roster_error,
                "ai_status": ai_status,
                "roster_cache_age": f"{(datetime.datetime.now() - (attendance_processor.roster_last_updated or datetime.datetime.now())).seconds} seconds" if attendance_processor.roster_last_updated else "Not cached yet",
                "zoom_verification": zoom_tokens_status
            }
        }
    except Exception as e:
        return {"status": "error", "message": f"Error generating debug info: {str(e)}"}

@app.get("/roster")
async def get_roster():
    """Endpoint to get the roster (for testing)."""
    roster = await attendance_processor.get_roster(force_refresh=True)
    return {"roster_count": len(roster), "first_few": roster[:5]}

@app.post("/refresh-roster")
async def refresh_roster():
    """Force-refresh the roster cache."""
    roster = await attendance_processor.get_roster(force_refresh=True)
    return {"status": "success", "message": f"Roster refreshed with {len(roster)} entries"}

@app.post("/test-matching")
async def test_matching(request: Request):
    """Test the Gemini AI name matching."""
    data = await request.json()
    name = data.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")

    roster = await attendance_processor.get_roster()

    results = {}

    # Try AI matching if enabled
    if config.USE_AI_MATCHING:
        try:
            ai_match = await attendance_processor.match_participant_with_roster(name, roster)
            results["ai_match"] = ai_match
        except Exception as e:
            results["ai_match_error"] = str(e)

    # Always include simple matching for comparison
    simple_match = attendance_processor.simple_name_matching(name, roster)
    results["simple_match"] = simple_match

    # Include person details if we have a match from either method
    person_id = None
    if config.USE_AI_MATCHING and "ai_match" in results and results["ai_match"].get("matchedPersonId"):
        person_id = results["ai_match"]["matchedPersonId"]
    elif results["simple_match"].get("matchedPersonId"):
        person_id = results["simple_match"]["matchedPersonId"]

    if person_id:
        person_details = next((p for p in roster if p.get("Id") == person_id), None)
        results["person_details"] = person_details

    return results

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8188)