export type Channel='group'|'wan'|'yan'|'liu';
export const names:Record<Channel,string>={group:'大家一起',wan:'小婉',yan:'小研',liu:'小柳'};
export interface Message {id:string;role:'user'|'assistant';content:string;speaker?:string;created_at:number;pending?:boolean}
export interface Auth {enabled:boolean;logged_in:boolean;username:string;scope:string;registration_mode:'closed'|'invite'|'open'}
export interface Profile {title:string;avatar:string;display_avatar?:string;default_avatar?:string}
export interface Activity {preview?:string;updated_at?:number;incoming_at?:number;unread?:number}
export interface Story {id:string;title:string;avatar:string;channels:Partial<Record<Channel,Profile>>;activity:Partial<Record<Channel,Activity>>}
export interface ModelConfig {base_url:string;model:string;api_key:string;temperature:number;max_tokens:number;embedding_model?:string;api_key_masked?:string}
export interface Settings extends ModelConfig {inherit_model:boolean;purpose:string;persona:string;relationship:string;public_persona:string;base_voice:string;reply_mode:'text'|'voice';goal_mode:boolean;speech_enabled:boolean;speech_configured:boolean;adult_mode:boolean}
export interface McpKey {id:string;label:string;scopes:string[];expires_at:number;revoked:number;daily_limit:number;calls:number;daily_tokens:number;used_tokens:number;reserved_tokens:number;estimated_calls:number}
